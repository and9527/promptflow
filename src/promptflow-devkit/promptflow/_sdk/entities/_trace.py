# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import copy
import datetime
import json
import logging
import typing
import uuid
from dataclasses import asdict, dataclass

from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PBSpan

from promptflow._constants import (
    RUNNING_LINE_RUN_STATUS,
    SPAN_EVENTS_ATTRIBUTES_EVENT_ID,
    SpanAttributeFieldName,
    SpanContextFieldName,
    SpanEventFieldName,
    SpanFieldName,
    SpanLinkFieldName,
    SpanResourceAttributesFieldName,
    SpanResourceFieldName,
    SpanStatusFieldName,
)
from promptflow._sdk._constants import (
    SPAN_EVENTS_ATTRIBUTE_PAYLOAD,
    SPAN_EVENTS_NAME_PF_INPUTS,
    SPAN_EVENTS_NAME_PF_OUTPUT,
    TRACE_DEFAULT_COLLECTION,
    CumulativeTokenCountFieldName,
)
from promptflow._sdk._errors import LineRunNotFoundError
from promptflow._sdk._orm.trace import Event as ORMEvent
from promptflow._sdk._orm.trace import LineRun as ORMLineRun
from promptflow._sdk._orm.trace import Span as ORMSpan
from promptflow._sdk._utils import (
    convert_time_unix_nano_to_timestamp,
    flatten_pb_attributes,
    parse_otel_span_status_code,
)


class Event:
    @staticmethod
    def get(event_id: str) -> typing.Dict:
        orm_event = ORMEvent.get(event_id)
        return json.loads(orm_event.data)


class Span:
    """Span is exactly the same as OpenTelemetry Span."""

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        name: str,
        context: typing.Dict[str, str],
        kind: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        status: typing.Dict[str, str],
        resource: typing.Dict,
        parent_id: typing.Optional[str] = None,
        attributes: typing.Optional[typing.Dict[str, str]] = None,
        links: typing.Optional[typing.List] = None,
        events: typing.Optional[typing.List] = None,
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.name = name
        self.context = copy.deepcopy(context)
        self.kind = kind
        self.parent_id = parent_id
        self.start_time = start_time
        self.end_time = end_time
        self.status = copy.deepcopy(status)
        self.attributes = copy.deepcopy(attributes) if attributes is not None else dict()
        self.links = copy.deepcopy(links) if links is not None else list()
        self.events = copy.deepcopy(events) if events is not None else list()
        self.resource = copy.deepcopy(resource)
        self._external_event_ids = list()

    def _persist(self) -> None:
        # persist (create or update) line run
        # line run should be persisted before events, where `events.attributes` will be updated inplace
        self._persist_line_run()
        # persist events to table `events`
        # this operation will update `events.attributes` inplace
        self._persist_events()
        # persist span
        self._to_orm_object().persist()

    def _persist_events(self) -> None:
        # persist events to table `events` and update `events.attributes` inplace
        for i in range(len(self.events)):
            event_id = str(uuid.uuid4())
            event = self.events[i]
            ORMEvent(
                event_id=event_id,
                trace_id=self.trace_id,
                span_id=self.span_id,
                data=json.dumps(event),
            ).persist()
            self.events[i][SpanEventFieldName.ATTRIBUTES] = {SPAN_EVENTS_ATTRIBUTES_EVENT_ID: event_id}

    def _load_events(self) -> None:
        # load events from table `events` and update `events.attributes` inplace
        events = []
        for i in range(len(self.events)):
            event_id = self.events[i][SpanEventFieldName.ATTRIBUTES][SPAN_EVENTS_ATTRIBUTES_EVENT_ID]
            self._external_event_ids.append(event_id)
            events.append(Event.get(event_id=event_id))
        self.events = events

    def _persist_line_run(self) -> None:
        # within a trace id, the line run will be created/updated in two cases:
        #   1. first span: create, as we cannot identify the first span, so will use a try-catch
        #   2. root span: update
        if self.parent_id is None:
            LineRun._from_root_span(self)._try_update()
        else:
            LineRun._from_non_root_span(self)._try_create()

    @staticmethod
    def _from_orm_object(obj: ORMSpan) -> "Span":
        return Span(
            trace_id=obj.trace_id,
            span_id=obj.span_id,
            name=obj.name,
            context=copy.deepcopy(obj.context),
            kind=obj.kind,
            parent_id=obj.parent_id,
            start_time=obj.start_time,
            end_time=obj.end_time,
            status=copy.deepcopy(obj.status),
            attributes=copy.deepcopy(obj.attributes),
            links=copy.deepcopy(obj.links),
            events=copy.deepcopy(obj.events),
            resource=copy.deepcopy(obj.resource),
        )

    def _to_orm_object(self) -> ORMSpan:
        return ORMSpan(
            trace_id=self.trace_id,
            span_id=self.span_id,
            name=self.name,
            context=copy.deepcopy(self.context),
            kind=self.kind,
            parent_id=self.parent_id,
            start_time=self.start_time,
            end_time=self.end_time,
            status=copy.deepcopy(self.status),
            attributes=copy.deepcopy(self.attributes) if len(self.attributes) > 0 else None,
            links=copy.deepcopy(self.links) if len(self.links) > 0 else None,
            events=copy.deepcopy(self.events) if len(self.events) > 0 else None,
            resource=copy.deepcopy(self.resource),
        )

    @staticmethod
    def _from_protobuf_events(obj: typing.List[PBSpan.Event]) -> typing.List[typing.Dict]:
        events = []
        if len(obj) == 0:
            return events
        for pb_event in obj:
            event_dict: dict = json.loads(MessageToJson(pb_event))
            event = {
                SpanEventFieldName.NAME: pb_event.name,
                # .isoformat() here to make this dumpable to JSON
                SpanEventFieldName.TIMESTAMP: convert_time_unix_nano_to_timestamp(pb_event.time_unix_nano).isoformat(),
                SpanEventFieldName.ATTRIBUTES: flatten_pb_attributes(
                    event_dict.get(SpanEventFieldName.ATTRIBUTES, dict())
                ),
            }
            events.append(event)
        return events

    @staticmethod
    def _from_protobuf_links(obj: typing.List[PBSpan.Link]) -> typing.List[typing.Dict]:
        links = []
        if len(obj) == 0:
            return links
        for pb_link in obj:
            link_dict: dict = json.loads(MessageToJson(pb_link))
            link = {
                SpanLinkFieldName.CONTEXT: {
                    SpanContextFieldName.TRACE_ID: pb_link.trace_id.hex(),
                    SpanContextFieldName.SPAN_ID: pb_link.span_id.hex(),
                    SpanContextFieldName.TRACE_STATE: pb_link.trace_state,
                },
                SpanLinkFieldName.ATTRIBUTES: flatten_pb_attributes(
                    link_dict.get(SpanLinkFieldName.ATTRIBUTES, dict())
                ),
            }
            links.append(link)
        return links

    @staticmethod
    def _from_protobuf_object(obj: PBSpan, resource: typing.Dict, logger: logging.Logger) -> "Span":
        # Open Telemetry does not provide official way to parse Protocol Buffer Span object
        # so we need to parse it manually relying on `MessageToJson`
        # reference: https://github.com/open-telemetry/opentelemetry-python/issues/3700#issuecomment-2010704554
        span_dict: dict = json.loads(MessageToJson(obj))
        logger.debug("Received span: %s, resource: %s", json.dumps(span_dict), json.dumps(resource))
        span_id = obj.span_id.hex()
        trace_id = obj.trace_id.hex()
        parent_id = obj.parent_span_id.hex()
        # we have observed in some scenarios, there is not `attributes` field
        attributes = flatten_pb_attributes(span_dict.get(SpanFieldName.ATTRIBUTES, dict()))
        links = Span._from_protobuf_links(obj.links)
        events = Span._from_protobuf_events(obj.events)

        return Span(
            trace_id=trace_id,
            span_id=span_id,
            name=obj.name,
            context={
                SpanContextFieldName.TRACE_ID: trace_id,
                SpanContextFieldName.SPAN_ID: span_id,
                SpanContextFieldName.TRACE_STATE: obj.trace_state,
            },
            kind=obj.kind,
            parent_id=parent_id if parent_id else None,
            start_time=convert_time_unix_nano_to_timestamp(obj.start_time_unix_nano),
            end_time=convert_time_unix_nano_to_timestamp(obj.end_time_unix_nano),
            status={
                SpanStatusFieldName.STATUS_CODE: parse_otel_span_status_code(obj.status.code),
                SpanStatusFieldName.DESCRIPTION: obj.status.message,
            },
            attributes=attributes,
            links=links,
            events=events,
            resource=resource,
        )

    def _to_rest_object(self) -> typing.Dict:
        rest_events = copy.deepcopy(self.events)
        # `self._external_event_ids` is empty indicates:
        #   1. span object is lazy load
        #   2. no external events
        # iterate `self.events` to move event id(s) to `external_event_data_uris` in this case
        # following the large data contract
        if len(self._external_event_ids) == 0:
            rest_external_event_data_uris = list()
            for i in range(len(rest_events)):
                event_id = rest_events[i][SpanEventFieldName.ATTRIBUTES].pop(SPAN_EVENTS_ATTRIBUTES_EVENT_ID)
                rest_external_event_data_uris.append(event_id)
        else:
            rest_external_event_data_uris = copy.deepcopy(self._external_event_ids)
        return {
            "name": self.name,
            "context": copy.deepcopy(self.context),
            "kind": self.kind,
            "parent_id": self.parent_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "status": copy.deepcopy(self.status),
            "attributes": copy.deepcopy(self.attributes),
            "links": copy.deepcopy(self.links),
            "events": rest_events,
            "resource": copy.deepcopy(self.resource),
            "external_event_data_uris": rest_external_event_data_uris,
        }


@dataclass
class LineRun:
    line_run_id: str
    trace_id: str
    root_span_id: typing.Optional[str]
    inputs: typing.Optional[typing.Dict]
    outputs: typing.Optional[typing.Dict]
    start_time: datetime.datetime
    end_time: typing.Optional[datetime.datetime]
    status: str
    duration: typing.Optional[float]
    name: typing.Optional[str]
    kind: str
    collection: str
    cumulative_token_count: typing.Optional[typing.Dict[str, int]] = None
    parent_id: typing.Optional[str] = None
    run: typing.Optional[str] = None
    line_number: typing.Optional[int] = None
    experiment: typing.Optional[str] = None
    session_id: typing.Optional[str] = None
    evaluations: typing.Optional[typing.Dict[str, "LineRun"]] = None

    @staticmethod
    def _determine_line_run_id(span: Span) -> str:
        # for test, use `attributes.line_run_id`
        # for batch run and others, directly use `trace_id`
        if SpanAttributeFieldName.LINE_RUN_ID in span.attributes:
            return span.attributes[SpanAttributeFieldName.LINE_RUN_ID]
        else:
            return span.trace_id

    @staticmethod
    def _determine_parent_id(span: Span) -> typing.Optional[str]:
        # for test, `attributes.referenced.line_run_id` should be the parent id
        # for batch run, we need to query line run with run name and line number
        # otherwise, there will be no parent id
        if SpanAttributeFieldName.REFERENCED_LINE_RUN_ID in span.attributes:
            return span.attributes[SpanAttributeFieldName.REFERENCED_LINE_RUN_ID]
        elif SpanAttributeFieldName.REFERENCED_BATCH_RUN_ID in span.attributes:
            line_run = ORMLineRun._get_with_run_and_line_number(
                run=span.attributes[SpanAttributeFieldName.REFERENCED_BATCH_RUN_ID],
                line_number=span.attributes[SpanAttributeFieldName.LINE_NUMBER],
            )
            return line_run.line_run_id if line_run is not None else None
        else:
            return None

    @staticmethod
    def _parse_common_args(span: Span) -> typing.Dict:
        line_run_id = LineRun._determine_line_run_id(span)
        resource_attributes = dict(span.resource.get(SpanResourceFieldName.ATTRIBUTES, dict()))
        collection = resource_attributes.get(SpanResourceAttributesFieldName.COLLECTION, TRACE_DEFAULT_COLLECTION)
        experiment = resource_attributes.get(SpanResourceAttributesFieldName.EXPERIMENT_NAME, None)
        run = span.attributes.get(SpanAttributeFieldName.BATCH_RUN_ID, None)
        line_number = span.attributes.get(SpanAttributeFieldName.LINE_NUMBER, None)
        session_id = span.attributes.get(SpanAttributeFieldName.SESSION_ID, None)
        parent_id = LineRun._determine_parent_id(span)
        return {
            "line_run_id": line_run_id,
            "trace_id": span.trace_id,
            "start_time": span.start_time,
            "collection": collection,
            "parent_id": parent_id,
            "run": run,
            "line_number": line_number,
            "experiment": experiment,
            "session_id": session_id,
        }

    @staticmethod
    def _from_non_root_span(span: Span) -> "LineRun":
        common_args = LineRun._parse_common_args(span)
        return LineRun(
            root_span_id=None,
            inputs=None,
            outputs=None,
            end_time=None,
            status=RUNNING_LINE_RUN_STATUS,
            duration=None,
            name=None,
            kind=None,
            **common_args,
        )

    @staticmethod
    def _from_root_span(span: Span) -> "LineRun":
        common_args = LineRun._parse_common_args(span)
        # calculate `cumulative_token_count`
        completion_token_count = int(span.attributes.get(SpanAttributeFieldName.COMPLETION_TOKEN_COUNT, 0))
        prompt_token_count = int(span.attributes.get(SpanAttributeFieldName.PROMPT_TOKEN_COUNT, 0))
        total_token_count = int(span.attributes.get(SpanAttributeFieldName.TOTAL_TOKEN_COUNT, 0))
        if total_token_count > 0:
            cumulative_token_count = {
                CumulativeTokenCountFieldName.COMPLETION: completion_token_count,
                CumulativeTokenCountFieldName.PROMPT: prompt_token_count,
                CumulativeTokenCountFieldName.TOTAL: total_token_count,
            }
        else:
            cumulative_token_count = None

        return LineRun(
            root_span_id=span.span_id,
            inputs=LineRun._get_inputs_from_span(span),
            outputs=LineRun._get_outputs_from_span(span),
            end_time=span.end_time,
            status=span.status[SpanStatusFieldName.STATUS_CODE],
            duration=(span.end_time - span.start_time).total_seconds(),
            name=span.name,
            kind=span.attributes.get(SpanAttributeFieldName.SPAN_TYPE, span.kind),
            cumulative_token_count=cumulative_token_count,
            **common_args,
        )

    def _try_create(self) -> None:
        # try to get via line run id first; if not found, create a new line run
        try:
            ORMLineRun.get(line_run_id=self.line_run_id)
        except LineRunNotFoundError:
            self._to_orm_object().persist()

    def _try_update(self) -> None:
        # try to get first; need to create, instead of update, for trace with only one root span
        try:
            ORMLineRun.get(line_run_id=self.line_run_id)
            self._to_orm_object()._update()
        except LineRunNotFoundError:
            self._to_orm_object().persist()

    @staticmethod
    def _get_inputs_from_span(span: Span) -> typing.Optional[typing.Dict]:
        for event in span.events:
            if event[SpanEventFieldName.NAME] == SPAN_EVENTS_NAME_PF_INPUTS:
                return json.loads(event[SpanEventFieldName.ATTRIBUTES][SPAN_EVENTS_ATTRIBUTE_PAYLOAD])
        return None

    @staticmethod
    def _get_outputs_from_span(span: Span) -> typing.Optional[typing.Dict]:
        for event in span.events:
            if event[SpanEventFieldName.NAME] == SPAN_EVENTS_NAME_PF_OUTPUT:
                return json.loads(event[SpanEventFieldName.ATTRIBUTES][SPAN_EVENTS_ATTRIBUTE_PAYLOAD])
        return None

    @staticmethod
    def _from_orm_object(obj: ORMLineRun) -> "LineRun":
        return LineRun(
            line_run_id=obj.line_run_id,
            trace_id=obj.trace_id,
            root_span_id=obj.root_span_id,
            inputs=copy.deepcopy(obj.inputs),
            outputs=copy.deepcopy(obj.outputs),
            start_time=obj.start_time,
            end_time=obj.end_time,
            status=obj.status,
            duration=obj.duration,
            name=obj.name,
            kind=obj.kind,
            cumulative_token_count=copy.deepcopy(obj.cumulative_token_count),
            parent_id=obj.parent_id,
            run=obj.run,
            line_number=obj.line_number,
            experiment=obj.experiment,
            session_id=obj.session_id,
            collection=obj.collection,
        )

    def _to_orm_object(self) -> ORMLineRun:
        return ORMLineRun(
            line_run_id=self.line_run_id,
            trace_id=self.trace_id,
            root_span_id=self.root_span_id,
            inputs=copy.deepcopy(self.inputs),
            outputs=copy.deepcopy(self.outputs),
            start_time=self.start_time,
            end_time=self.end_time,
            status=self.status,
            duration=self.duration,
            name=self.name,
            kind=self.kind,
            cumulative_token_count=copy.deepcopy(self.cumulative_token_count),
            parent_id=self.parent_id,
            run=self.run,
            line_number=self.line_number,
            experiment=self.experiment,
            session_id=self.session_id,
            collection=self.collection,
        )

    def _append_evaluations(self, evaluations: typing.List["LineRun"]) -> None:
        for evaluation in evaluations:
            if self.evaluations is None:
                self.evaluations = dict()
            eval_name = evaluation.run if evaluation.run is not None else evaluation.name
            self.evaluations[eval_name] = evaluation

    def _to_rest_object(self) -> typing.Dict:
        # datetime.datetime is not JSON serializable, so we need to take care of this
        # otherwise, Flask will raise and complain about this
        # line run's start/end time, and (optional) evaluations start/end time
        _self = copy.deepcopy(self)
        _self.start_time = _self.start_time.isoformat()
        _self.end_time = _self.end_time.isoformat() if self.end_time is not None else None
        # evaluations
        if _self.evaluations is not None:
            for eval_name in _self.evaluations:
                evaluation = _self.evaluations[eval_name]
                _self.evaluations[eval_name].start_time = evaluation.start_time.isoformat()
                _self.evaluations[eval_name].end_time = (
                    evaluation.end_time.isoformat() if evaluation.end_time is not None else None
                )
        return asdict(_self)
