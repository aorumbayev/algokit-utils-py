import base64
import dataclasses
import json
import logging
import re
from enum import Enum

from algosdk.atomic_transaction_composer import AccountTransactionSigner, TransactionSigner
from algosdk.logic import get_application_address
from algosdk.transaction import StateSchema, SuggestedParams
from algosdk.v2client.algod import AlgodClient
from algosdk.v2client.indexer import IndexerClient
from pyteal import CallConfig

from algokit_utils.application_client import ApplicationClient
from algokit_utils.application_specification import ApplicationSpecification
from algokit_utils.models import Account

logger = logging.getLogger(__name__)

DEFAULT_INDEXER_MAX_API_RESOURCES_PER_ACCOUNT = 1000
UPDATABLE_TEMPLATE_NAME = "TMPL_UPDATABLE"
DELETABLE_TEMPLATE_NAME = "TMPL_DELETABLE"

NOTE_PREFIX = "APP NOTE:"
# when base64 encoding bytes, 3 bytes are stored in every 4 characters so assert the
# NOTE_PREFIX length is a multiple of 3, so we don't need to worry about the padding/changing characters at the end
assert len(NOTE_PREFIX) % 3 == 0


class DeploymentFailedError(Exception):
    pass


@dataclasses.dataclass
class AppNote:
    name: str
    version: str
    deletable: bool = dataclasses.field(kw_only=True)
    updatable: bool = dataclasses.field(kw_only=True)

    @staticmethod
    def from_json(value: str) -> "AppNote":
        return AppNote(**json.loads(value))

    @classmethod
    def from_b64(cls: type["AppNote"], b64: str) -> "AppNote":
        return cls.decode(base64.b64decode(b64))

    @classmethod
    def decode(cls: type["AppNote"], value: bytes) -> "AppNote":
        note = value.decode("utf-8")
        assert note.startswith(NOTE_PREFIX)
        return cls.from_json(note[len(NOTE_PREFIX) :])

    def encode(self) -> bytes:
        json_str = json.dumps(self.__dict__)
        return f"{NOTE_PREFIX}{json_str}".encode()


@dataclasses.dataclass
class App:
    id: int
    address: str
    created_at_round: int
    note: AppNote


def get_creator_apps(indexer: IndexerClient, creator_account: Account | str) -> dict[str, App]:
    apps: dict[str, App] = {}

    creator_address = creator_account if isinstance(creator_account, str) else creator_account.address
    token = None
    # TODO: abstract pagination logic?
    while True:
        response = indexer.lookup_account_application_by_creator(
            creator_address, limit=DEFAULT_INDEXER_MAX_API_RESOURCES_PER_ACCOUNT, next_page=token
        )  # type: ignore[no-untyped-call]
        if "message" in response:  # an error occurred
            raise Exception(f"Error querying applications for {creator_address}: {response}")
        for app in response["applications"]:
            app_id = app["id"]
            app_created_at_round = app["created-at-round"]
            update_transactions_response = indexer.search_transactions(
                min_round=app_created_at_round,
                txn_type="appl",
                application_id=app_id,
                note_prefix=NOTE_PREFIX.encode("utf-8"),
            )  # type: ignore[no-untyped-call]
            # created_transactions_response = indexer.search_transactions(
            #    min_round=app_created_at_round, max_round=app_created_at_round, txn_type="appl", application_id=app_id,
            # )  # type: ignore[no-untyped-call]
            update_transactions: list[dict] = update_transactions_response["transactions"][:]
            # creation_transaction = next(
            #    t
            #    for t in created_transactions_response["transactions"]
            #    if t["application-transaction"]["application-id"] == 0 and t["sender"] == creator_address
            # )
            # update_transactions.append(creation_transaction)

            def sort_by_round(transaction: dict) -> tuple[int, int]:
                confirmed = transaction["confirmed-round"]
                offset = transaction["intra-round-offset"]
                return confirmed, offset

            update_transactions.sort(key=sort_by_round, reverse=True)
            latest_transaction = update_transactions[0]

            note_b64 = latest_transaction.get("note")
            if not note_b64:
                continue

            try:
                app_note = AppNote.from_b64(note_b64)
            except json.decoder.JSONDecodeError:
                continue
            apps[app_note.name] = App(app_id, get_application_address(app_id), app_created_at_round, app_note)

        token = response.get("next-token")
        if not token:
            break

    return apps


# TODO: put these somewhere more useful
def _state_schema(schema: dict[str, int]) -> StateSchema:
    return StateSchema(schema.get("num-uint", 0), schema.get("num-byte-slice", 0))  # type: ignore[no-untyped-call]


def _schema_is_less(a: StateSchema, b: StateSchema) -> bool:
    return bool(a.num_uints < b.num_uints or a.num_byte_slices < b.num_byte_slices)


def _schema_str(global_schema: StateSchema, local_schema: StateSchema) -> str:
    return (
        f"Global: uints={global_schema.num_uints}, byte_slices={global_schema.num_byte_slices}, "
        f"Local: uints={local_schema.num_uints}, byte_slices={local_schema.num_byte_slices}"
    )


def get_application_client(
    algod_client: AlgodClient,
    indexer_client: IndexerClient,
    creator_account: Account | str,
    app_spec: ApplicationSpecification,
    *,
    signer: TransactionSigner | None = None,
    sender: str | None = None,
    suggested_params: SuggestedParams | None = None,
) -> ApplicationClient | None:
    creator_address = creator_account if isinstance(creator_account, str) else creator_account.address
    apps = get_creator_apps(indexer_client, creator_address)

    app_name = app_spec.contract.name
    app = apps.get(app_name)
    if not app:
        logger.info(f"Could not find app {app_name} in account {creator_address}.")
        return None

    logger.info(f"{app_name} ({app.note.version}) found in {creator_address} account with app id {app.id}.")

    return ApplicationClient(
        algod_client, app_spec, app_id=app.id, signer=signer, sender=sender, suggested_params=suggested_params
    )


def _replace_template_variable(program_lines: list[str], template_variable: str, value: str) -> tuple[list[str], int]:
    result: list[str] = []
    match_count = 0
    pattern = re.compile(rf"(\b){template_variable}(\b|$)")

    def replacement(m: re.Match) -> str:
        return f"{m.group(1)}{value}{m.group(2)}"

    for line in program_lines:
        code_comment = line.split("//", maxsplit=1)
        code = code_comment[0]
        new_line, matches = re.subn(pattern, replacement, code)
        match_count += matches
        if len(code_comment) > 1:
            new_line = f"{new_line}//{code_comment[1]}"
        result.append(new_line)
    return result, match_count


def _merge_template_variables(
    template_values: dict[str, int | str] | None, allow_update: bool | None, allow_delete: bool | None
) -> dict[str, int | str]:
    merged_template_values: dict[str, int | str] = {f"TMPL_{k}": v for k, v in (template_values or {}).items()}
    if allow_update is not None:
        merged_template_values[UPDATABLE_TEMPLATE_NAME] = int(allow_update)
    if allow_delete is not None:
        merged_template_values[DELETABLE_TEMPLATE_NAME] = int(allow_delete)
    return merged_template_values


def _check_template_variables(approval_program: str, template_values: dict[str, int | str]) -> None:
    if UPDATABLE_TEMPLATE_NAME in approval_program and UPDATABLE_TEMPLATE_NAME not in template_values:
        raise DeploymentFailedError(
            "allow_update must be specified if deploy time configuration of update is being used"
        )
    if DELETABLE_TEMPLATE_NAME in approval_program and DELETABLE_TEMPLATE_NAME not in template_values:
        raise DeploymentFailedError(
            "allow_delete must be specified if deploy time configuration of delete is being used"
        )

    for template_variable_name in template_values:
        if template_variable_name not in approval_program:
            if template_variable_name == UPDATABLE_TEMPLATE_NAME:
                raise DeploymentFailedError(
                    "allow_update must only be specified if deploy time configuration of update is being used"
                )
            elif template_variable_name == DELETABLE_TEMPLATE_NAME:
                raise DeploymentFailedError(
                    "allow_delete must only be specified if deploy time configuration of delete is being used"
                )
            else:
                logger.warning(f"{template_variable_name} not found in approval program, but variable was provided")


def replace_template_variables(program: str, template_values: dict[str, int | str]) -> str:
    program_lines = program.splitlines()
    for template_variable_name, template_value in template_values.items():
        value = str(template_value) if isinstance(template_value, int) else "0x" + template_value.encode("utf-8").hex()
        program_lines, matches = _replace_template_variable(program_lines, template_variable_name, value)

    result = "\n".join(program_lines)
    return result


class OnUpdate(Enum):
    Fail = 0
    UpdateApp = 1
    DeleteApp = 2


class OnSchemaBreak(Enum):
    Fail = 0
    DeleteApp = 2


# TODO: split this function up
def deploy_app(
    algod_client: AlgodClient,
    indexer_client: IndexerClient,
    app_spec: ApplicationSpecification,
    creator_account: Account,
    version: str,
    *,
    on_update: OnUpdate = OnUpdate.UpdateApp,
    on_schema_break: OnSchemaBreak = OnSchemaBreak.Fail,
    allow_update: bool | None = None,
    allow_delete: bool | None = None,
    template_values: dict[str, int | str] | None = None,
) -> App:
    # TODO: return ApplicationClient

    approval_template_values = _merge_template_variables(template_values, allow_update, allow_delete)
    _check_template_variables(app_spec.approval_program, approval_template_values)

    # make a copy
    app_spec = ApplicationSpecification.from_json(app_spec.to_json())
    app_spec.approval_program = replace_template_variables(app_spec.approval_program, approval_template_values)
    app_spec.clear_program = replace_template_variables(app_spec.clear_program, template_values or {})
    # add blueprint for these
    updatable = (
        allow_update
        if allow_update is not None
        else (
            app_spec.bare_call_config.update_application != CallConfig.NEVER
            or any(h for h in app_spec.hints.values() if h.call_config.update_application != CallConfig.NEVER)
        )
    )

    deletable = (
        allow_delete
        if allow_delete is not None
        else (
            app_spec.bare_call_config.delete_application != CallConfig.NEVER
            or any(h for h in app_spec.hints.values() if h.call_config.delete_application != CallConfig.NEVER)
        )
    )

    name = app_spec.contract.name
    app_spec_note = AppNote(name, version, updatable=updatable, deletable=deletable)

    # TODO: allow resolve app id via environment variable
    apps = get_creator_apps(indexer_client, creator_account)
    app = apps.get(name)
    app_id = app.id if app else 0
    app_client = ApplicationClient(
        algod_client, app_spec, app_id=app_id, signer=AccountTransactionSigner(creator_account.private_key)
    )

    def create_app() -> App:
        create_result = app_client.create(note=app_spec_note.encode())
        logger.info(f"{name} ({version}) deployed successfully, with app id {create_result.app_id}.")
        return App(create_result.app_id, create_result.app_address, create_result.confirmed_round, app_spec_note)

    if app is None:
        logger.info(f"{name} not found in {creator_account.address} account, deploying app.")
        return create_app()

    logger.debug(
        f"{name} found in {creator_account.address} account, with app id {app.id}, version={app.note.version}."
    )
    app_client = ApplicationClient(
        algod_client, app_spec, app_id=app.id, signer=AccountTransactionSigner(creator_account.private_key)
    )

    application_info = indexer_client.applications(app.id)  # type: ignore[no-untyped-call]
    application_create_params = application_info["application"]["params"]

    current_approval = base64.b64decode(application_create_params["approval-program"])
    current_clear = base64.b64decode(application_create_params["clear-state-program"])
    current_global_schema = _state_schema(application_create_params["global-state-schema"])
    current_local_schema = _state_schema(application_create_params["local-state-schema"])

    required_global_schema = app_spec.global_state_schema
    required_local_schema = app_spec.local_state_schema
    new_approval = app_client.approval.raw_binary
    new_clear = app_client.clear.raw_binary

    app_updated = current_approval != new_approval or current_clear != new_clear

    schema_breaking_change = _schema_is_less(current_global_schema, required_global_schema) or _schema_is_less(
        current_local_schema, required_local_schema
    )

    def create_and_delete_app() -> App:
        logger.info(f"Deploying {name} ({version}) in {creator_account.address} account.")

        new_app = create_app()

        # delete the old app
        assert app
        logger.info(f"Deleting {name} ({app.note.version}) in {creator_account.address} account, with app id {app.id}")

        # TODO: passing the new app_spec to delete the old app doesn't seem right... (even tho it works)
        old_app_client = ApplicationClient(
            algod_client, app_spec, app_id=app.id, signer=AccountTransactionSigner(creator_account.private_key)
        )
        old_app_client.delete()

        return new_app

    def update_app() -> App:
        assert on_update == OnUpdate.UpdateApp
        assert app
        logger.info(f"Updating {name} to {version} in {creator_account.address} account, with app id {app.id}")

        update_result = app_client.update(note=app_spec_note.encode())
        return App(update_result.app_id, update_result.app_address, update_result.confirmed_round, app_spec_note)

    if schema_breaking_change:
        logger.warning(
            f"Detected a breaking app schema change from: "
            f"{_schema_str(current_global_schema, current_local_schema)} to "
            f"{_schema_str(required_global_schema, required_local_schema)}."
        )

        if on_schema_break == OnSchemaBreak.Fail:
            raise DeploymentFailedError(
                "Schema break detected and on_schema_break=OnSchemaBreak.Fail, stopping deployment. "
                "If you want to try deleting and recreating the app then "
                "re-run with on_schema_break=OnSchemaBreak.DeleteApp"
            )
        if app.note.deletable:
            logger.info(
                "App is deletable and on_schema_break=DeleteApp, will attempt to create new app and delete old app"
            )
        else:
            logger.warning(
                "App is not deletable but on_schema_break=DeleteApp, "
                "will attempt to delete app, delete will most likely fail"
            )
        return create_and_delete_app()
    elif app_updated:
        logger.info(f"Detected a TEAL update in app id {app.id}")

        if on_update == OnUpdate.Fail:
            raise DeploymentFailedError(
                "Update detected and on_update=Fail, stopping deployment. "
                "If you want to try updating the app then re-run with on_update=UpdateApp"
            )
        if app.note.updatable and on_update == OnUpdate.UpdateApp:
            logger.info("App is updatable and on_update=UpdateApp, will update app")
            return update_app()
        elif app.note.updatable and on_update == OnUpdate.DeleteApp:
            logger.warning(
                "App is updatable but on_update=DeleteApp, will attempt to create new app and delete old app"
            )
            return create_and_delete_app()
        elif on_update == OnUpdate.DeleteApp:
            logger.warning(
                "App is not updatable and on_update=DeleteApp, will attempt to create new app and delete old app"
            )
            return create_and_delete_app()
        else:
            logger.warning(
                "App is not updatable but on_update=UpdateApp, "
                "will attempt to update app, update will most likely fail"
            )
            return update_app()

    logger.info("No detected changes in app, nothing to do.")

    return app
