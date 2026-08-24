"""A live run, declared: the tool, its args, the gate answers, the assertions.

The harness drives the daemon exactly as the plugin does - ``dev-tool-invoke``
on a registered tool, then answering each card as it arrives - and hands back
:class:`RunEvidence`: the tool's status, the layers that landed on the canvas,
the cards that actually fired, and the artifacts the run wrote to its OWN prefix.

Nothing is re-derived. The physical answer a test asserts on is read back out of
the run's ``metrics.json``, and the chart is the spec the run persisted, so an
assertion cites the product rather than a second implementation of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trid3nt_contracts import new_ulid

from .ws_client import (
    BLOCKING_EVENTS,
    WS_URL,
    approve_confirmation,
    create_case,
    delete_case,
    handshake,
    mk,
    parse_tool_status,
)

logger = logging.getLogger("trid3nt_server.testing.live_run")

__all__ = ["GateAnswers", "LiveRun", "RunEvidence", "drive", "run_live"]

#: What a run writes under its own prefix once it has an answer to record.
_RUN_PRODUCTS = (("chart_spec", "chart_spec.json"), ("metrics", "metrics.json"))


class LiveRunError(AssertionError):
    """A declared expectation the live run did not meet."""


@dataclass(frozen=True)
class GateAnswers:
    """How this run answers the cards it expects to be shown.

    A declared answer is also an EXPECTATION: ``require_draw`` / ``require_form``
    turn a card that never appeared into a failure rather than a silent pass, so
    a test cannot claim to have exercised a gate it never saw.
    """

    #: The geometry a DRAW card is answered with. A point is ``[lon, lat]``; a
    #: polyline/polygon is a list of vertices. ``None`` declines the card.
    draw: Sequence[float] | Sequence[Sequence[float]] | None = None
    draw_geometry: str = "point"
    #: Rows to revise on a FORM card, submitted as ``narrow_scope`` +
    #: ``revised_args``. Empty submits the sheet unchanged.
    form_edits: Mapping[str, Any] = field(default_factory=dict)
    #: What a plain (sheet-less) payload warning is answered with.
    confirm: str = "proceed"
    require_draw: bool = False
    require_form: bool = False


@dataclass
class RunEvidence:
    """What the run actually did - the record a report cites."""

    tool: str
    args: dict[str, Any]
    session_id: str
    case_id: str | None = None
    tool_status: str | None = None
    #: A tool-io frame arrived, i.e. the tool actually DISPATCHED. Separate from
    #: ``tool_status`` because a tool returning a typed layer has no ``status``
    #: field at all - "no status" is success there and a dead turn otherwise.
    dispatched: bool = False
    is_error: bool = False
    detail: str = ""
    turn_complete: bool = False
    charts: int = 0
    #: The chart payloads that crossed the wire. For a template whose product IS
    #: the chart (a schematic deck publishes no raster, so there is no run prefix
    #: to read back), this is the run's own persisted product and the only honest
    #: thing an assertion can cite.
    chart_payloads: list[dict[str, Any]] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)
    draw_card: dict[str, Any] | None = None
    form_card: dict[str, Any] | None = None
    plain_warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    chart_spec: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    product_uris: dict[str, str] = field(default_factory=dict)
    product_errors: dict[str, str] = field(default_factory=dict)

    # -- assertions ------------------------------------------------------- #
    def require_ok(self) -> "RunEvidence":
        """The tool dispatched and did not fail.

        A tool that returns a TYPED result (a ``LayerURI`` subtype) carries no
        ``status`` field, so ``tool_status is None`` alongside a delivered tool-io
        frame IS success. Requiring the literal ``"ok"`` would only ever pass for
        tools that answer with a status dict - which is the failure shape.
        """
        if not self.dispatched:
            raise LiveRunError(
                f"{self.tool} never dispatched - no tool-io frame arrived: "
                f"{self.detail or '(no detail)'}")
        if self.is_error or self.tool_status == "error":
            raise LiveRunError(
                f"{self.tool} failed (status={self.tool_status!r}): "
                f"{self.detail or '(no detail)'}")
        if not self.turn_complete:
            # A turn that stopped on an unanswered blocking event or ran out the
            # clock left the run unfinished; the tool-io frame it did emit is not
            # a completed run, and reading it as one is how a half-run passes.
            raise LiveRunError(
                f"{self.tool}: the turn never completed - "
                f"{self.detail or 'no turn-complete frame arrived'}")
        return self

    def require_layer(self, *, name_contains: str = "", layer_type: str = "",
                      role: str = "") -> dict[str, Any]:
        for layer in self.layers:
            if name_contains and name_contains.lower() not in str(
                    layer.get("name", "")).lower():
                continue
            if layer_type and layer.get("layer_type") != layer_type:
                continue
            if role and layer.get("role") != role:
                continue
            return layer
        raise LiveRunError(
            f"{self.tool}: no layer matching name~{name_contains!r} "
            f"type={layer_type!r} role={role!r} among "
            f"{[l.get('name') for l in self.layers]}")

    def require_chart(self, *, title_contains: str = "") -> dict[str, Any]:
        """A chart the run actually emitted - the product, not a rebuild of it."""
        for payload in self.chart_payloads:
            if title_contains and title_contains.lower() not in str(
                    payload.get("title", "")).lower():
                continue
            return payload
        raise LiveRunError(
            f"{self.tool}: no chart matching title~{title_contains!r} among "
            f"{[p.get('title') for p in self.chart_payloads]}")

    def require_run_products(self) -> "RunEvidence":
        if not self.run_id:
            raise LiveRunError(f"{self.tool}: no run prefix to read products from "
                               f"({self.product_errors})")
        missing = [k for k, _ in _RUN_PRODUCTS if getattr(self, k) is None]
        if missing:
            raise LiveRunError(
                f"{self.tool} run {self.run_id}: {missing} absent from the run "
                f"prefix ({self.product_errors})")
        return self

    def metric(self, name: str) -> Any:
        if not self.metrics:
            raise LiveRunError(f"{self.tool}: no metrics.json to read {name!r} from")
        if name not in self.metrics:
            raise LiveRunError(
                f"{self.tool}: metrics.json has no {name!r} (has "
                f"{sorted(self.metrics)})")
        return self.metrics[name]

    def require_metric_close(self, name: str, expected: float,
                             *, rel: float = 1e-6) -> "RunEvidence":
        got = float(self.metric(name))
        if abs(got - float(expected)) > abs(float(expected)) * rel + 1e-12:
            raise LiveRunError(
                f"{self.tool}: {name}={got!r} is not within {rel} of {expected!r}")
        return self

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class LiveRun:
    """One declared invocation: tool + args + gate answers + how long to wait."""

    tool: str
    args: Mapping[str, Any]
    case_title: str
    answers: GateAnswers = field(default_factory=GateAnswers)
    timeout_s: float = 1800.0
    #: Delete the Case afterwards. A throwaway proof Case cleans up; a showcase
    #: Case is the user's and is never touched.
    cleanup_case: bool = False


async def drive(run: LiveRun) -> RunEvidence:
    """Invoke the tool over the live socket, answering its cards, and record it."""
    import websockets.asyncio.client as wsc

    session_id = new_ulid()
    ev = RunEvidence(tool=run.tool, args=dict(run.args), session_id=session_id)
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await handshake(ws, session_id)
        ev.case_id = await create_case(ws, session_id, run.case_title)
        await ws.send(mk("dev-tool-invoke", session_id,
                         {"name": run.tool, "args": dict(run.args),
                          "case_id": ev.case_id,
                          "raw_text": f"!run {run.tool}(...)"},
                         case_id=ev.case_id))
        await _pump(ws, session_id, run, ev)
        if run.cleanup_case:
            await delete_case(ws, session_id, ev.case_id)
    _read_run_products(ev)
    _check_declared_cards(run.answers, ev)
    return ev


async def _pump(ws: Any, session_id: str, run: LiveRun, ev: RunEvidence) -> None:
    deadline = time.monotonic() + run.timeout_s
    latest_layers: list[dict[str, Any]] = []
    activity = False
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=min(deadline - time.monotonic(), 45))
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        kind = msg["type"]
        if kind == "spatial-input-request":
            activity = True
            await _answer_draw(ws, session_id, msg, run.answers, ev)
        elif kind == "tool-payload-warning":
            activity = True
            await _answer_warning(ws, session_id, msg, run.answers, ev)
        elif kind == "confirmation-request":
            activity = True
            await approve_confirmation(ws, session_id, msg)
        elif kind == "chart-emission":
            ev.charts += 1
            payload = msg["payload"]
            if isinstance(payload, dict):
                ev.chart_payloads.append(payload)
        elif kind in BLOCKING_EVENTS:
            ev.detail = f"BLOCKED by {kind} - the run declares no answer for it"
            break
        elif kind == "tool-io":
            activity = True
            ev.dispatched = True
            ev.tool_status = parse_tool_status(msg["payload"])
            if msg["payload"].get("is_error"):
                ev.is_error = True
                ev.detail = (msg["payload"].get("function_response") or "")[:600]
        elif kind == "session-state":
            loaded = msg["payload"].get("loaded_layers") or []
            if loaded:
                latest_layers = loaded
        elif kind == "error":
            ev.detail = (f"{msg['payload'].get('error_code')}: "
                         f"{msg['payload'].get('message')}")
            break
        elif kind == "turn-complete" and activity:
            ev.turn_complete = True
            break
    # EMISSION ORDER, recorded: ``loaded_layers`` is the emitter's append-ordered
    # list (a re-publish replaces in place, so a layer keeps its first-emission
    # slot) and each row carries its ``z_index``. The last non-empty snapshot is
    # therefore the whole canvas in the order the seams delivered it - which is
    # what an order-faithful proof render reads.
    ev.layers = latest_layers


async def _answer_draw(ws: Any, session_id: str, msg: dict[str, Any],
                       answers: GateAnswers, ev: RunEvidence) -> None:
    payload = msg["payload"]
    drawn = answers.draw
    ev.draw_card = {
        "request_id": payload.get("request_id"),
        "mode": payload.get("mode"),
        "purpose": payload.get("purpose"),
        "title": payload.get("title"),
        "description": payload.get("description"),
        "answered_with": list(drawn) if drawn is not None else None,
        "declined": drawn is None,
    }
    if drawn is None:
        await ws.send(mk("spatial-input-response", session_id, {
            "request_id": payload["request_id"], "geometry_type": None,
            "coordinates": None, "features": None, "cancelled": True}))
        return
    await ws.send(mk("spatial-input-response", session_id, {
        "request_id": payload["request_id"],
        "geometry_type": answers.draw_geometry,
        "coordinates": list(drawn) if answers.draw_geometry in ("point", "bbox")
        else None,
        "features": (None if answers.draw_geometry in ("point", "bbox")
                     else _feature_collection(answers.draw_geometry, drawn)),
        "cancelled": False}))


def _feature_collection(geometry: str, vertices: Any) -> dict[str, Any]:
    """A drawn shape in the shape the vertex-capture tool submits."""
    coords = [[float(v[0]), float(v[1])] for v in vertices]
    if geometry == "polygon":
        geom = {"type": "Polygon", "coordinates": [coords]}
    else:
        geom = {"type": "LineString", "coordinates": coords}
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": geom}]}


async def _answer_warning(ws: Any, session_id: str, msg: dict[str, Any],
                          answers: GateAnswers, ev: RunEvidence) -> None:
    payload = msg["payload"]
    sheet = payload.get("param_sheet")
    if isinstance(sheet, dict) and sheet.get("rows"):
        # An edit for a row the card does not carry would be submitted, silently
        # ignored by the re-seat, and then asserted about - so it is a declaration
        # error, caught against the sheet the server actually rendered.
        unknown = sorted(set(answers.form_edits)
                         - {str(row.get("name")) for row in sheet["rows"]})
        if unknown:
            raise LiveRunError(
                f"{ev.tool}: form_edits name {unknown}, which the card does not "
                f"carry (rows: {sorted(str(r.get('name')) for r in sheet['rows'])})")
        ev.form_card = {
            "workflow": sheet.get("workflow"),
            "title": sheet.get("title"),
            "rows": [{k: row.get(k) for k in
                      ("name", "value", "units", "door", "basis", "source_badge",
                       "bounds", "advanced", "note")}
                     for row in sheet["rows"]],
            "edited": dict(answers.form_edits),
        }
        # SUBMIT IS THE APPROVAL: the whole sheet was on screen, so an edited
        # submit proceeds rather than re-presenting the table the user just filled.
        await ws.send(mk("tool-payload-confirmation", session_id, {
            "warning_id": payload["warning_id"],
            "decision": "narrow_scope" if answers.form_edits else "proceed",
            "revised_args": dict(answers.form_edits) or None}))
        return
    ev.plain_warnings.append(str(payload.get("tool_name")))
    await ws.send(mk("tool-payload-confirmation", session_id, {
        "warning_id": payload["warning_id"],
        "decision": answers.confirm, "revised_args": None}))


def _check_declared_cards(answers: GateAnswers, ev: RunEvidence) -> None:
    if answers.require_draw and ev.draw_card is None:
        raise LiveRunError(
            f"{ev.tool}: the run declares a DRAW answer but no draw card fired")
    if answers.require_form and ev.form_card is None:
        raise LiveRunError(
            f"{ev.tool}: the run declares FORM edits but no param sheet fired")


def _read_run_products(ev: RunEvidence) -> None:
    """Pull the run's OWN artifacts off its prefix - never a rederivation."""
    # The PRIMARY raster, and ONLY it: a run that surfaces its fetched inputs
    # through the emit-on-fetch seam puts context rasters on the canvas ahead of
    # its result, and those live under the cache bucket - so falling back to any
    # raster reports a prefix (`cache`) the run never wrote to, which reads as
    # "the run's products are missing" rather than "this was the wrong prefix".
    rasters = [l for l in ev.layers if l.get("layer_type") == "raster"
               and str(l.get("uri", "")).startswith("s3://")]
    raster = next((l for l in rasters if l.get("role") == "primary"), None)
    if raster is None:
        ev.product_errors["run_id"] = (
            "no published PRIMARY raster to locate the run prefix"
            + (f" ({len(rasters)} context raster(s) on the canvas)" if rasters else ""))
        return
    bucket, _, key = str(raster["uri"])[len("s3://"):].partition("/")
    ev.run_id = key.split("/", 1)[0]

    import boto3

    s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                      region_name=os.environ.get("AWS_REGION", "us-east-1"))
    for label, name in _RUN_PRODUCTS:
        try:
            blob = s3.get_object(Bucket=bucket,
                                 Key=f"{ev.run_id}/{name}")["Body"].read()
            setattr(ev, label, json.loads(blob))
            ev.product_uris[label] = f"s3://{bucket}/{ev.run_id}/{name}"
        except Exception as exc:  # noqa: BLE001 - absence IS the finding
            ev.product_errors[label] = f"{type(exc).__name__}: {exc}"


def run_live(run: LiveRun) -> RunEvidence:
    """Synchronous entry point for a driver script."""
    return asyncio.run(drive(run))
