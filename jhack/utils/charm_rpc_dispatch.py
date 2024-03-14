import base64
import importlib
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Type

import ops
import ops.storage
from ops.main import (
    CHARM_STATE_FILE,
    CharmMeta,
    JujuVersion,
    _Dispatcher,
    _get_charm_dir,
    _should_use_controller_storage,
    setup_root_logging,
)


def _deserialize_env(s: str) -> Dict[str, str]:
    env = dict((pair.split("=") for pair in s.split(" ")))
    return env


ENV: Dict[str, str] = _deserialize_env(os.getenv("CHARM_RPC_ENV"))
MODULE_NAME = os.getenv("CHARM_RPC_MODULE_NAME")  # string
ENTRYPOINT = os.getenv("CHARM_RPC_ENTRYPOINT")  # string
LOGLEVEL = os.getenv("CHARM_RPC_LOGLEVEL")  # string
EVAL_EXPR = os.getenv("CHARM_RPC_EXPR")  # string
CHARM_NAME = os.getenv("CHARM_RPC_CHARM_NAME")  # string

logger = logging.getLogger("charm-rpc")
logger.setLevel(LOGLEVEL)


def _decode(expr: str) -> str:
    return base64.b64decode(expr.encode("utf-8")).decode("ascii")


def rpc(charm):
    if MODULE_NAME:
        logger.debug("running rpc in script mode")

        # load module
        module = importlib.import_module(MODULE_NAME)
        entrypoint = getattr(module, ENTRYPOINT)
        logger.debug(
            f"found entrypoint {ENTRYPOINT!r} in crpc script. Invoking on charm..."
        )
        return_value = entrypoint(charm)
        return return_value

    elif EVAL_EXPR:
        expr = _decode(EVAL_EXPR)
        logger.debug(f"running rpc in eval-expr mode: \n" f"expr={expr!r}")
        try:
            return eval(expr, {"self": charm, "ops": ops})
        except Exception:  # noqa
            logger.exception(f"failed executing {expr!r} in with self={charm}.")


def ops_main_rpc(charm_class: Type[ops.charm.CharmBase], use_juju_for_storage: bool):
    # inject the juju envvars we need
    os.environ.update(ENV)

    charm_dir = _get_charm_dir()

    model_backend = ops.model._ModelBackend()
    debug = "JUJU_DEBUG" in os.environ
    setup_root_logging(model_backend, debug=debug)
    logger.debug("charm rpc dispatch v0.1 up and running.")

    dispatcher = _Dispatcher(charm_dir)
    metadata = (charm_dir / "metadata.yaml").read_text()
    actions_meta = charm_dir / "actions.yaml"
    if actions_meta.exists():
        actions_metadata = actions_meta.read_text()
    else:
        actions_metadata = None

    meta = CharmMeta.from_yaml(metadata, actions_metadata)
    model = ops.model.Model(meta, model_backend)

    charm_state_path = charm_dir / CHARM_STATE_FILE

    if use_juju_for_storage and not ops.storage.juju_backend_available():
        # raise an exception; the charm is broken and needs fixing.
        msg = "charm set use_juju_for_storage=True, but Juju version {} does not support it"
        raise RuntimeError(msg.format(JujuVersion.from_environ()))

    if use_juju_for_storage is None:
        use_juju_for_storage = _should_use_controller_storage(charm_state_path, meta)

    if use_juju_for_storage:
        store = ops.storage.JujuStorage()
    else:
        store = ops.storage.SQLiteStorage(charm_state_path)
    framework = ops.framework.Framework(
        store, charm_dir, meta, model, event_name=dispatcher.event_name
    )
    framework.set_breakpointhook()
    charm = charm_class(framework)

    logger.debug(f"instantiated charm {charm.__class__.__name__}")
    return charm


def check_controller_storage(charm_type: Type[ops.charm.CharmBase]) -> bool:
    # try to guesstimate if the charm is using controller storage or not.
    # unfortunately this is probably best done in the hackiest way possible
    charm_source = Path(inspect.getmodule(charm_type).__file__).read_text()

    # I literally had to go and wash my hands after writing this
    if (
        "use_juju_for_storage=True" in charm_source
        or "use_juju_for_storage = True" in charm_source
    ):
        return True
    return False


def load_charm_type() -> Type[ops.charm.CharmBase]:
    charm_name: Optional[str] = CHARM_NAME
    raise ValueError("PWD: " + os.getcwd())
    module = importlib.import_module("charm")
    charm_subclasses = [
        (identifier, obj)
        for identifier, obj in module.__dict__.items()
        if (
            isinstance(obj, type)
            and issubclass(obj, ops.charm.CharmBase)
            and identifier != "CharmBase"
        )
    ]

    logger.debug(f"found charm types {charm_subclasses}")

    if charm_name:
        by_name = [obj for i, obj in charm_subclasses if i == charm_name]
    else:
        by_name = [obj for _, obj in charm_subclasses]

    if len(by_name) < 1:
        if charm_name and charm_subclasses:
            options = ", ".join((a[0] for a in charm_subclasses))
            raise RuntimeError(
                f"couldn't find any charm type called "
                f"{charm_name!r} in charm.py; only {options!r}"
            )
        raise RuntimeError("couldn't find any charm type in charm.py")

    if len(by_name) > 1 and not charm_name:
        options = ", ".join((a[0] for a in charm_subclasses))

        raise RuntimeError(
            "Multiple charm types found! Pass a `--charm-name` "
            f"to help us narrow down the search. Found: {options}."
        )

    return by_name[0]


def main():
    charm_type = load_charm_type()
    use_juju_for_storage = check_controller_storage(charm_type)
    charm = ops_main_rpc(charm_type, use_juju_for_storage=use_juju_for_storage)
    output = rpc(charm)

    if output is not None:
        print(output)

    sys.exit(0)


if __name__ == "__main__":
    main()
