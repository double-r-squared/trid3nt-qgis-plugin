"""Submit portable reviewed runs and relay their results over the daemon WebSocket."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import tempfile
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from trid3nt.credentials.auth_handshake import configured_access_token
from trid3nt.credentials.resolver import remote_runner_credential
from trid3nt.errors import DeclarationError
from trid3nt.sdk.case import case
from trid3nt.sdk.deck_state import decode, encode
from trid3nt.sdk.lock import Lock, canonical, served_digest
from trid3nt.sdk.run_messages import RunEvent, RunInstruction, StopRun
from trid3nt.sdk.script_run import ReviewRequest, ReviewResponse, save_deck
from trid3nt.settings import setting
from trid3nt.store import objects
from trid3nt.workflows.runtime.journal import append_record
from trid3nt.workflows.runtime.script import Executed
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.solver.solver import DOCKER_KILL_TIMEOUT_S
from trid3nt.workflows.solver.stop import Stopped

from . import script_child

PATH = "/runner"
TRANSPORT_LOG = logging.getLogger("trid3nt.runner.websocket")
TRANSPORT_LOG.setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def runner_url() -> str:
    return os.environ.get("TRID3NT_REMOTE_RUNNER_URL", "").strip()


def enabled() -> bool:
    return os.environ.get("TRID3NT_REMOTE_RUNNER", "").strip() == "1"


def authorized(header: str) -> bool:
    expected = remote_runner_credential()
    plugin = configured_access_token()
    return bool(expected and expected != plugin and hmac.compare_digest(header.encode(), f"Bearer {expected}".encode()))


def gate(connection, request, *, accepting: bool):
    if request.path != PATH:
        return None
    if not accepting:
        return connection.respond(HTTPStatus.NOT_FOUND, "Runner disabled.")
    values = request.headers.get_all("Authorization")
    if len(values) != 1 or not authorized(values[0]):
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Runner credential refused.")
    return None


def _refuse(code: str, message: str = "The remote reviewed run could not finish.") -> DeclarationError:
    return DeclarationError(message, error_code=code)


def _path(root: Path, rel: str) -> Path:
    relative = PurePosixPath(rel)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise _refuse("REMOTE_INSTRUCTION_INVALID")
    return root.joinpath(*relative.parts)


def _download(uri: str, digest: str, target: Path) -> None:
    _, bucket, key = objects.split_object_uri(uri)
    target.parent.mkdir(parents=True, exist_ok=True)
    objects.served_client().download_file(bucket, key, str(target))
    if objects.sha256_of(target) != digest:
        raise _refuse("DIGEST_MISMATCH", "A served case file differs from its pin.")


def _bundle(path: Path, deck, receipt: dict, case_id: str) -> RunInstruction:
    lock = Lock(path.parent)
    prefix = lock.data["served"] + "/runner"
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".runner-") as temporary:
        root = Path(temporary)
        for name in (path.name, "trid3nt.lock"):
            shutil.copy2(path.with_name(name), root / name)
        if path.with_name("versions").is_dir():
            shutil.copytree(path.with_name("versions"), root / "versions")
        packed = case(root)
        packed.pack()
        staged = deck.staged
        reviewed = lock.data["models"][deck.state.workflow.name]["deck"]["files"]
        pins = {}
        for item in staged.get("inputs", ()):
            uri = item["gs_uri"]
            if not uri.startswith("s3://"):
                raise _refuse("REMOTE_INPUT_UNPINNED")
            pins[uri] = reviewed[item["dest"]]
        if manifest_uri := staged.get("manifest_uri"):
            _, bucket, key = objects.split_object_uri(manifest_uri)
            with objects.client().get_object(Bucket=bucket, Key=key)["Body"] as body:
                manifest = body.read()
            expected = {"run_id": deck.run_id, **{key: staged[key] for key in ("case", "inputs", "outputs")}}
            if canonical(json.loads(manifest)) != canonical(expected):
                raise _refuse("DIGEST_MISMATCH", "The worker manifest differs from the reviewed deck.")
            pins[manifest_uri] = hashlib.sha256(manifest).hexdigest()

        def pin(uri: str) -> str:
            _, bucket, key = objects.split_object_uri(uri)
            if bucket != "data" and key and not key.endswith("/") and uri not in pins:
                pins[uri] = served_digest(uri)
            return uri

        locations = {}

        def portable_path(source: Path) -> str:
            source = source.resolve()
            if source in locations:
                return locations[source]
            label = f"launch-files/{len(locations)}/{source.name}"
            locations[source] = label
            destination = root / label
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            elif source.is_file():
                shutil.copy2(source, destination)
            else:
                raise _refuse("REMOTE_STATE_UNSUPPORTED")
            return label

        graph = encode(deck, deck.state.workflow, portable_path, uri=pin)
        (root / "launch.json").write_bytes(canonical({"state": graph, "tool": deck.state.workflow.name,
                                                     "receipt": receipt, "settings": deck.state.settings}))
        packed.link(prefix)
        files = {p.relative_to(root).as_posix(): objects.sha256_of(p) for p in root.rglob("*")
                 if p.is_file() and not p.name.startswith(".")}
    return RunInstruction(case=case_id, run_id=deck.run_id, script=path.name, served=prefix,
                          files=files, inputs=pins)


def _prepare(root: Path, instruction: RunInstruction) -> tuple[Path, dict, float | None]:
    if set(("launch.json", "trid3nt.lock", instruction.script)) - instruction.files.keys():
        raise _refuse("REMOTE_INSTRUCTION_INVALID")
    for relative, digest in instruction.files.items():
        _download(f"{instruction.served}/{relative}", digest, _path(root, relative))
    launch = json.loads((root / "launch.json").read_bytes())
    local = case(root)
    model = getattr(local, launch["tool"])()
    workflow = model._workflow
    deck = decode(launch["state"], workflow, lambda name: _path(root, name))
    if deck.run_id != instruction.run_id:
        raise _refuse("REMOTE_INSTRUCTION_INVALID")
    path = root / instruction.script
    cores = int(setting("LOCAL_CORES"))
    settings = {key: value for key, value in launch["settings"].items()
                if key not in {"LOCAL_CORES", "CONTAINER_CORES", "SOLVE_BUDGET_S"}}
    settings.update(LOCAL_CORES=cores, CONTAINER_CORES=int(setting("CONTAINER_CORES")),
                    SOLVE_BUDGET_S=setting("SOLVE_BUDGET_S"))
    handoff = script_child._handoff(path, deck, launch["receipt"], instruction.case, cores)
    handoff.update(remote_inputs=instruction.inputs, runner=socket.gethostname(), settings=settings,
                   instruction=instruction.model_dump(mode="json"))
    return path, handoff, float(settings["SOLVE_BUDGET_S"]) or None


def _serve_study(root: Path, instruction: RunInstruction) -> dict[str, str]:
    if instruction.script != "calibrate.py":
        return {}
    case_root = root.parents[1]
    name = f"cal-{instruction.run_id}"
    files = Lock(case_root).data["studies"][name]
    _, bucket, prefix = objects.split_object_uri(instruction.served)
    store = objects.served_client()
    for relative, digest in files.items():
        path = _path(case_root, relative)
        if objects.sha256_of(path) != digest:
            raise _refuse("DIGEST_MISMATCH")
        with path.open("rb") as body:
            store.put_object(Bucket=bucket, Key=f"{prefix}/{relative}", Body=body)
    return files


def _keep_study(path: Path, instruction: RunInstruction, files: dict[str, str]) -> None:
    from trid3nt.sdk.study import pin_archived_base

    root = path.parent.parents[1]
    name = f"cal-{instruction.run_id}"
    target = root / "studies" / name
    if not all(key.startswith(f"studies/{name}/") for key in files):
        raise _refuse("REMOTE_INSTRUCTION_INVALID")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".remote-study-") as temporary:
        scratch = Path(temporary)
        for relative, digest in files.items():
            _download(f"{instruction.served}/{relative}", digest, _path(scratch, relative))
        downloaded = scratch / "studies" / name
        study = json.loads((downloaded / "study.json").read_bytes())
        with (target.parent / ".publish").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            lock = Lock(root)
            if target.exists():
                raise _refuse("STUDY_EXISTS")
            pin_archived_base(Lock(path.parent), lock, study["base_version"])
            downloaded.rename(target)
            try:
                lock.data.setdefault("studies", {})[name] = files
                lock.save()
            except BaseException:
                target.rename(downloaded)
                raise


class _Forward(script_child._Relay):
    def __init__(self, websocket):
        super().__init__(None)
        self.socket = websocket
        self.replies = {}
        self.stopped = None

    async def event(self, kind, *args):
        if kind == "checked":
            self.checked = True
        if kind == "dispatch":
            self.run_id = getattr(args[2], "run_id", self.run_id)
        if kind == "terminal" and args[0] is None:
            self.cancelling.set()
        if kind in {"complete", "stopped", "refused"}:
            if kind == "stopped":
                self.stopped = args
            return kind, *args
        await self.socket.send(RunEvent.event(kind, *args).model_dump_json())
        return None

    async def answer(self, request):
        reply = asyncio.get_running_loop().create_future()
        self.replies[request.id] = reply
        try:
            await self.socket.send(RunEvent.event("review", request.model_dump(mode="json")).model_dump_json())
            return (await reply).outcome()
        finally:
            self.replies.pop(request.id)

    async def publish(self, record):
        pass

    async def abort(self, code, cancelled):
        pass


async def serve(websocket) -> None:
    """Run one authenticated instruction and listen for decisions until it ends."""
    forward = _Forward(websocket)
    task = listener = None

    async def commands():
        async for raw in websocket:
            message = json.loads(raw)
            if message.get("kind") == "stop":
                StopRun.model_validate(message)
                logger.info("Runner received Stop for %s", instruction.run_id)
                task.cancel()
                return
            reply = ReviewResponse.model_validate(message)
            waiting = forward.replies.get(reply.id)
            if waiting is not None and not waiting.done():
                waiting.set_result(reply)

    try:
        instruction = RunInstruction.model_validate_json(await asyncio.wait_for(websocket.recv(), 10))
        if instruction.case is None:
            raise _refuse("REMOTE_INSTRUCTION_INVALID")
        home = Path(os.environ.get("TRID3NT_HOME") or Path.home() / ".trid3nt")
        root = home / "cases" / str(instruction.case) / "runs" / str(instruction.run_id)
        root.mkdir(parents=True, exist_ok=False)

        async def run():
            path, handoff, budget = await asyncio.to_thread(_prepare, root, instruction)
            return await script_child.run_child(path, handoff, forward, str(instruction.run_id), budget)

        task = asyncio.create_task(run())
        listener = asyncio.create_task(commands())
        ready, _ = await asyncio.wait((task, listener), return_when=asyncio.FIRST_COMPLETED)
        if listener in ready and task not in ready and not task.cancelling():
            task.cancel()
        try:
            executed = await task
            files = await asyncio.to_thread(_serve_study, root, instruction)
            outcome = RunEvent.event("complete", executed.value, executed.record, files)
        except Stopped as stopped:
            outcome = RunEvent.event("stopped", *(forward.stopped or (stopped.value, stopped.why, None)))
        except asyncio.CancelledError:
            outcome = RunEvent.event("stopped", None, "stopped by the user", None)
        await websocket.send(outcome.model_dump_json())
    except ConnectionClosed:
        pass
    except Exception as error:  # noqa: BLE001 - one invocation has one terminal reply
        code = getattr(error, "error_code", "REMOTE_INSTRUCTION_INVALID")
        try:
            await websocket.send(RunEvent.event("refused", code, str(error) if code == "ENGINE_IMAGE_MISSING"
                                               else "The remote reviewed run refused.").model_dump_json())
        except ConnectionClosed:
            pass
    finally:
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _events(websocket, relay):
    async for raw in websocket:
        kind, *args = RunEvent.model_validate_json(raw).unpack()
        if kind == "review":
            request = ReviewRequest.model_validate(args[0])
            relay.review = asyncio.create_task(relay.answer(request))
            try:
                outcome = await relay.review
            except asyncio.CancelledError:
                continue
            finally:
                relay.review = None
            await websocket.send(ReviewResponse.from_outcome(request.id, outcome).model_dump_json())
            continue
        ended = await relay.event(kind, *args)
        if ended is not None:
            return ended
    raise _refuse("REMOTE_DISCONNECTED")


async def _stop_remote(websocket, relay, reader):
    await websocket.send(StopRun().model_dump_json())
    acknowledgement = asyncio.create_task(relay.cancelling.wait())
    try:
        ready, _ = await asyncio.wait((reader, acknowledgement),
                                      timeout=2 * DOCKER_KILL_TIMEOUT_S,
                                      return_when=asyncio.FIRST_COMPLETED)
        if not ready:
            raise _refuse("REMOTE_STOP_UNCONFIRMED")
        return await reader
    finally:
        acknowledgement.cancel()


async def _receive_stopped(websocket, relay, reader) -> Stopped:
    async def collect():
        ended = await _stop_remote(websocket, relay, reader)
        if ended[0] != "stopped":
            raise asyncio.CancelledError
        _, value, reason, record = ended
        if record is not None and relay.checked:
            await asyncio.to_thread(append_record, record)
            await relay.publish(record)
        await relay.abort("SCRIPT_CHILD_STOPPED", cancelled=True)
        return Stopped(RunResult(value=value), value, reason, recorded=True)

    settling = asyncio.create_task(collect())
    while True:
        try:
            return await asyncio.shield(settling)
        except asyncio.CancelledError:
            if settling.done():
                return settling.result()


async def submit(url, path, deck, receipt, relay) -> Executed:
    """Send one served case and relay its reviewed results on one authenticated connection."""
    from trid3nt.render.emission import current_turn_case

    token = remote_runner_credential()
    if not token:
        raise _refuse("REMOTE_CREDENTIAL_MISSING", "The paired runner credential is missing.")
    parsed = urlsplit(url)
    if (parsed.scheme != "wss" and not (parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", PATH}):
        raise _refuse("REMOTE_RUNNER_URL_INVALID")
    try:
        async with connect(parsed._replace(path=PATH).geturl(), additional_headers={"Authorization": f"Bearer {token}"},
                           open_timeout=10, max_size=64 * 1024 * 1024, logger=TRANSPORT_LOG) as websocket:
            try:
                instruction = await asyncio.to_thread(_bundle, path, deck, receipt, current_turn_case())
            except DeclarationError:
                raise
            except objects.DigestMismatchError as error:
                raise _refuse("DIGEST_MISMATCH", "A served case file differs from its pin.") from error
            except Exception as error:
                raise _refuse("REMOTE_BUNDLE_FAILED") from error
            await websocket.send(instruction.model_dump_json())
            reader = asyncio.create_task(_events(websocket, relay))
            try:
                ended = await asyncio.shield(reader)
            except asyncio.CancelledError:
                relay.cancel_review()

                raise await _receive_stopped(websocket, relay, reader) from None
            finally:
                if not reader.done():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
    except InvalidStatus as error:
        status = error.response.status_code
        raise _refuse("REMOTE_CREDENTIAL_REJECTED" if status == 401 else "REMOTE_RUNNER_DISABLED"
                      if status == 404 else "REMOTE_UNREACHABLE") from None
    except (OSError, TimeoutError, WebSocketException):
        raise _refuse("REMOTE_UNREACHABLE") from None

    if ended[0] == "refused":
        await relay.abort(ended[1], cancelled=False)
        raise _refuse(*ended[1:])
    kind, value, record, *transferred = ended
    if kind != "complete" or record is None or not relay.checked:
        raise _refuse("SCRIPT_RECORD_MISSING")
    if transferred and transferred[0]:
        await asyncio.to_thread(_keep_study, path, instruction, transferred[0])
    await asyncio.to_thread(append_record, record)
    await relay.publish(record)
    return Executed(value, record)
