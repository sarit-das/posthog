from enum import Enum
import json
from typing import List, Dict, Literal
from posthog.models.entity.entity import Entity as LegacyEntity
from posthog.schema import (
    ActionsNode,
    BaseMathType,
    BreakdownFilter,
    ChartDisplayType,
    DateRange,
    EventsNode,
    FunnelExclusionActionsNode,
    FunnelExclusionEventsNode,
    FunnelsFilter,
    FunnelsQuery,
    LifecycleFilter,
    LifecycleQuery,
    PathsFilter,
    PathsQuery,
    PropertyGroupFilter,
    RetentionFilter,
    RetentionQuery,
    StickinessFilter,
    StickinessQuery,
    TrendsFilter,
    TrendsQuery,
)
from posthog.types import InsightQueryNode


class MathAvailability(str, Enum):
    Unavailable = ("Unavailable",)
    All = ("All",)
    ActorsOnly = "ActorsOnly"


actors_only_math_types = [
    BaseMathType.dau,
    BaseMathType.weekly_active,
    BaseMathType.monthly_active,
    Literal["unique_group"],
    Literal["hogql"],
]


def is_property_with_operator(property: Dict):
    return property.get("type") not in ("cohort", "hogql")


def clean_property(property: Dict):
    cleaned_property = {**property}

    # fix type typo
    if cleaned_property.get("type") == "events":
        cleaned_property["type"] = "event"

    # fix value key typo
    if cleaned_property.get("values") is not None and cleaned_property.get("value") is None:
        cleaned_property["value"] = cleaned_property.pop("values")

    # convert precalculated and static cohorts to cohorts
    if cleaned_property.get("type") in ("precalculated-cohort", "static-cohort"):
        cleaned_property["type"] = "cohort"

    # fix invalid property key for cohorts
    if cleaned_property.get("type") == "cohort" and cleaned_property.get("key") != "id":
        cleaned_property["key"] = "id"

    # set a default operator for properties that support it, but don't have an operator set
    if is_property_with_operator(cleaned_property) and cleaned_property.get("operator") is None:
        cleaned_property["operator"] = "exact"

    # remove the operator for properties that don't support it, but have it set
    if not is_property_with_operator(cleaned_property) and cleaned_property.get("operator") is not None:
        del cleaned_property["operator"]

    # remove none from values
    if isinstance(cleaned_property.get("value"), List):
        cleaned_property["value"] = list(filter(lambda x: x is not None, cleaned_property.get("value")))

    # remove keys without concrete value
    cleaned_property = {key: value for key, value in cleaned_property.items() if value is not None}

    return cleaned_property


# old style dict properties
def is_old_style_properties(properties):
    return isinstance(properties, Dict) and len(properties) == 1 and properties.get("type") not in ("AND", "OR")


def transform_old_style_properties(properties):
    key = list(properties.keys())[0]
    value = list(properties.values())[0]
    key_split = key.split("__")
    return [
        {
            "key": key_split[0],
            "value": value,
            "operator": key_split[1] if len(key_split) > 1 else "exact",
            "type": "event",
        }
    ]


def clean_entity_properties(properties: List[Dict] | None):
    if properties is None:
        return None
    elif is_old_style_properties(properties):
        return transform_old_style_properties(properties)
    else:
        return list(map(clean_property, properties))


def clean_property_group_filter_value(value: Dict):
    if value.get("type") in ("AND", "OR"):
        value["values"] = map(clean_property_group_filter_value, value.get("values"))
        return value
    else:
        return clean_property(value)


def clean_properties(properties: Dict):
    properties["values"] = map(clean_property_group_filter_value, properties.get("values"))
    return properties


def clean_display(display: str):
    if display not in ChartDisplayType.__members__:
        return None
    else:
        return display


def legacy_entity_to_node(
    entity: LegacyEntity, include_properties: bool, math_availability: MathAvailability
) -> EventsNode | ActionsNode:
    """
    Takes a legacy entity and converts it into an EventsNode or ActionsNode.
    """
    shared = {
        "name": entity.name,
        "custom_name": entity.custom_name,
    }

    if include_properties:
        shared = {
            **shared,
            "properties": clean_entity_properties(entity._data.get("properties", None)),
        }

    if math_availability != MathAvailability.Unavailable:
        #  only trends and stickiness insights support math.
        #  transition to then default math for stickiness, when an unsupported math type is encountered.

        if (
            entity.math is not None
            and math_availability == MathAvailability.ActorsOnly
            and entity.math not in actors_only_math_types
        ):
            shared = {**shared, "math": BaseMathType.dau}
        else:
            shared = {
                **shared,
                "math": entity.math,
                "math_property": entity.math_property,
                "math_hogql": entity.math_hogql,
                "math_group_type_index": entity.math_group_type_index,
            }

    if entity.type == "actions":
        return ActionsNode(id=entity.id, **shared)
    else:
        return EventsNode(event=entity.id, **shared)


def exlusion_entity_to_node(entity) -> FunnelExclusionEventsNode | FunnelExclusionActionsNode:
    """
    Takes a legacy exclusion entity and converts it into an FunnelExclusionEventsNode or FunnelExclusionActionsNode.
    """
    base_entity = legacy_entity_to_node(
        LegacyEntity(entity), include_properties=False, math_availability=MathAvailability.Unavailable
    )
    if isinstance(base_entity, EventsNode):
        return FunnelExclusionEventsNode(
            **base_entity.model_dump(),
            funnelFromStep=entity.get("funnel_from_step"),
            funnelToStep=entity.get("funnel_to_step"),
        )
    else:
        return FunnelExclusionActionsNode(
            **base_entity.model_dump(),
            funnelFromStep=entity.get("funnel_from_step"),
            funnelToStep=entity.get("funnel_to_step"),
        )


# TODO: remove this method that returns legacy entities
def to_base_entity_dict(entity: Dict):
    return {
        "type": entity.get("type"),
        "id": entity.get("id"),
        "name": entity.get("name"),
        "custom_name": entity.get("custom_name"),
        "order": entity.get("order"),
    }


insight_to_query_type = {
    "TRENDS": TrendsQuery,
    "FUNNELS": FunnelsQuery,
    "RETENTION": RetentionQuery,
    "PATHS": PathsQuery,
    "LIFECYCLE": LifecycleQuery,
    "STICKINESS": StickinessQuery,
}

INSIGHT_TYPE = Literal["TRENDS", "FUNNELS", "RETENTION", "PATHS", "LIFECYCLE", "STICKINESS"]


def _date_range(filter: Dict):
    date_range = DateRange(date_from=filter.get("date_from"), date_to=filter.get("date_to"))

    if len(date_range.model_dump(exclude_defaults=True)) == 0:
        return {}

    return {"dateRange": date_range}


def _interval(filter: Dict):
    if _insight_type(filter) == "RETENTION" or _insight_type(filter) == "PATHS":
        return {}

    if filter.get("interval") == "minute":
        return {"interval": "hour"}

    return {"interval": filter.get("interval")}


def _series(filter: Dict):
    if _insight_type(filter) == "RETENTION" or _insight_type(filter) == "PATHS":
        return {}

    # remove templates gone wrong
    if filter.get("events") is not None:
        filter["events"] = [event for event in filter.get("events") if not (isinstance(event, str))]

    math_availability: MathAvailability = MathAvailability.Unavailable
    include_properties: bool = True

    if _insight_type(filter) == "TRENDS":
        math_availability = MathAvailability.All
    elif _insight_type(filter) == "STICKINESS":
        math_availability = MathAvailability.ActorsOnly

    return {
        "series": [
            legacy_entity_to_node(entity, include_properties, math_availability)
            for entity in _entities(filter)
            if entity.id is not None
        ]
    }


def _entities(filter: Dict):
    processed_entities: List[LegacyEntity] = []

    # add actions
    actions = filter.get("actions", [])
    if isinstance(actions, str):
        actions = json.loads(actions)
    processed_entities.extend([LegacyEntity({**entity, "type": "actions"}) for entity in actions])

    # add events
    events = filter.get("events", [])
    if isinstance(events, str):
        events = json.loads(events)
    processed_entities.extend([LegacyEntity({**entity, "type": "events"}) for entity in events])

    # order by order
    processed_entities.sort(key=lambda entity: entity.order if entity.order else -1)

    # set sequential index values on entities
    for index, entity in enumerate(processed_entities):
        entity.index = index

    return processed_entities


def _sampling_factor(filter: Dict):
    if isinstance(filter.get("sampling_factor"), str):
        try:
            return float(filter.get("sampling_factor"))
        except (ValueError, TypeError):
            return {}
    else:
        return {"samplingFactor": filter.get("sampling_factor")}


def _filter_test_accounts(filter: Dict):
    return {"filterTestAccounts": filter.get("filter_test_accounts")}


def _properties(filter: Dict):
    raw_properties = filter.get("properties", None)
    if raw_properties is None or len(raw_properties) == 0:
        return {}
    elif isinstance(raw_properties, list):
        raw_properties = {
            "type": "AND",
            "values": [{"type": "AND", "values": raw_properties}],
        }
        return {"properties": PropertyGroupFilter(**clean_properties(raw_properties))}
    elif is_old_style_properties(raw_properties):
        raw_properties = transform_old_style_properties(raw_properties)
        raw_properties = {
            "type": "AND",
            "values": [{"type": "AND", "values": raw_properties}],
        }
        return {"properties": PropertyGroupFilter(**clean_properties(raw_properties))}
    else:
        return {"properties": PropertyGroupFilter(**clean_properties(raw_properties))}


def _breakdown_filter(_filter: Dict):
    if _insight_type(_filter) != "TRENDS" and _insight_type(_filter) != "FUNNELS":
        return {}

    # early return for broken breakdown filters
    if _filter.get("breakdown_type") == "undefined" and not isinstance(_filter.get("breakdown"), str):
        return {}

    breakdownFilter = {
        "breakdown_type": _filter.get("breakdown_type"),
        "breakdown": _filter.get("breakdown"),
        "breakdown_normalize_url": _filter.get("breakdown_normalize_url"),
        "breakdown_group_type_index": _filter.get("breakdown_group_type_index"),
        "breakdown_hide_other_aggregation": _filter.get("breakdown_hide_other_aggregation"),
        "breakdown_histogram_bin_count": _filter.get("breakdown_histogram_bin_count")
        if _insight_type(_filter) == "TRENDS"
        else None,
    }

    # fix breakdown typo
    if breakdownFilter["breakdown_type"] == "events":
        breakdownFilter["breakdown_type"] = "event"

    if _filter.get("breakdowns") is not None:
        if len(_filter.get("breakdowns")) == 1:
            breakdownFilter["breakdown_type"] = _filter.get("breakdowns")[0].get("type", None)
            breakdownFilter["breakdown"] = _filter.get("breakdowns")[0].get("property", None)
        else:
            raise Exception("Could not convert multi-breakdown property `breakdowns` - found more than one breakdown")

    if breakdownFilter["breakdown"] is not None and breakdownFilter["breakdown_type"] is None:
        breakdownFilter["breakdown_type"] = "event"

    if isinstance(breakdownFilter["breakdown"], list):
        breakdownFilter["breakdown"] = list(filter(lambda x: x is not None, breakdownFilter["breakdown"]))

    if len(BreakdownFilter(**breakdownFilter).model_dump(exclude_defaults=True)) == 0:
        return {}

    return {"breakdownFilter": BreakdownFilter(**breakdownFilter)}


def _group_aggregation_filter(filter: Dict):
    if _insight_type(filter) == "STICKINESS" or _insight_type(filter) == "LIFECYCLE":
        return {}
    return {"aggregation_group_type_index": filter.get("aggregation_group_type_index")}


def _insight_filter(filter: Dict):
    if _insight_type(filter) == "TRENDS":
        insight_filter = {
            "trendsFilter": TrendsFilter(
                smoothingIntervals=filter.get("smoothing_intervals"),
                showLegend=filter.get("show_legend"),
                # hidden_legend_indexes=cleanHiddenLegendIndexes(filter.get('hidden_legend_keys')),
                compare=filter.get("compare"),
                aggregationAxisFormat=filter.get("aggregation_axis_format"),
                aggregationAxisPrefix=filter.get("aggregation_axis_prefix"),
                aggregationAxisPostfix=filter.get("aggregation_axis_postfix"),
                decimalPlaces=filter.get("decimal_places"),
                formula=filter.get("formula"),
                display=clean_display(filter.get("display")),
                showValuesOnSeries=filter.get("show_values_on_series"),
                showPercentStackView=filter.get("show_percent_stack_view"),
                showLabelsOnSeries=filter.get("show_label_on_series"),
            )
        }
    elif _insight_type(filter) == "FUNNELS":
        insight_filter = {
            "funnelsFilter": FunnelsFilter(
                funnelVizType=filter.get("funnel_viz_type"),
                funnelOrderType=filter.get("funnel_order_type"),
                funnelFromStep=filter.get("funnel_from_step"),
                funnelToStep=filter.get("funnel_to_step"),
                funnelWindowIntervalUnit=filter.get("funnel_window_interval_unit"),
                funnelWindowInterval=filter.get("funnel_window_interval"),
                funnelStepReference=filter.get("funnel_step_reference"),
                breakdownAttributionType=filter.get("breakdown_attribution_type"),
                breakdownAttributionValue=filter.get("breakdown_attribution_value"),
                binCount=filter.get("bin_count"),
                exclusions=[exlusion_entity_to_node(entity) for entity in filter.get("exclusions", [])],
                layout=filter.get("layout"),
                # hidden_legend_breakdowns: cleanHiddenLegendSeries(filter.get('hidden_legend_keys')),
                funnelAggregateByHogQL=filter.get("funnel_aggregate_by_hogql"),
            ),
        }
    elif _insight_type(filter) == "RETENTION":
        insight_filter = {
            "retentionFilter": RetentionFilter(
                retentionType=filter.get("retention_type"),
                retentionReference=filter.get("retention_reference"),
                totalIntervals=filter.get("total_intervals"),
                returningEntity=to_base_entity_dict(filter.get("returning_entity"))
                if filter.get("returning_entity") is not None
                else None,
                targetEntity=to_base_entity_dict(filter.get("target_entity"))
                if filter.get("target_entity") is not None
                else None,
                period=filter.get("period"),
            )
        }
    elif _insight_type(filter) == "PATHS":
        insight_filter = {
            "pathsFilter": PathsFilter(
                pathsHogQLExpression=filter.get("paths_hogql_expression"),
                includeEventTypes=filter.get("include_event_types"),
                startPoint=filter.get("start_point"),
                endPoint=filter.get("end_point"),
                pathGroupings=filter.get("path_groupings"),
                excludeEvents=filter.get("exclude_events"),
                stepLimit=filter.get("step_limit"),
                pathReplacements=filter.get("path_replacements"),
                localPathCleaningFilters=filter.get("local_path_cleaning_filters"),
                edgeLimit=filter.get("edge_limit"),
                minEdgeWeight=filter.get("min_edge_weight"),
                maxEdgeWeight=filter.get("max_edge_weight"),
                funnelPaths=filter.get("funnel_paths"),
                funnelFilter=filter.get("funnel_filter"),
            )
        }
    elif _insight_type(filter) == "LIFECYCLE":
        insight_filter = {
            "lifecycleFilter": LifecycleFilter(
                toggledLifecycles=filter.get("toggledLifecycles"),
                showValuesOnSeries=filter.get("show_values_on_series"),
            )
        }
    elif _insight_type(filter) == "STICKINESS":
        insight_filter = {
            "stickinessFilter": StickinessFilter(
                compare=filter.get("compare"),
                showLegend=filter.get("show_legend"),
                # hidden_legend_indexes: cleanHiddenLegendIndexes(filter.get('hidden_legend_keys')),
                showValuesOnSeries=filter.get("show_values_on_series"),
            )
        }
    else:
        raise Exception(f"Invalid insight type {filter.get('insight')}.")

    if len(list(insight_filter.values())[0].model_dump(exclude_defaults=True)) == 0:
        return {}

    return insight_filter


def _insight_type(filter: Dict) -> INSIGHT_TYPE:
    if filter.get("insight") == "SESSIONS":
        return "TRENDS"
    return filter.get("insight", "TRENDS")


def filter_to_query(filter: Dict) -> InsightQueryNode:
    Query = insight_to_query_type[_insight_type(filter)]

    data = {
        **_date_range(filter),
        **_interval(filter),
        **_series(filter),
        **_sampling_factor(filter),
        **_filter_test_accounts(filter),
        **_properties(filter),
        **_breakdown_filter(filter),
        **_group_aggregation_filter(filter),
        **_insight_filter(filter),
    }

    return Query(**data)


def filter_str_to_query(filters: str) -> InsightQueryNode:
    filter = json.loads(filters)
    # we have insights that have been serialized to json twice in the database
    # due to people misunderstanding our api
    if isinstance(filter, str):
        filter = json.loads(filter)
    # we also have insights wrapped in an additional array
    elif isinstance(filter, list):
        filter = filter[0]
    return filter_to_query(filter)
