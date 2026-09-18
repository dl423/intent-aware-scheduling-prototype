"""Natural-language intent extraction and explicit-field precedence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import AttentionProfile, QualityFloor
from .providers import LUNA_MODEL, TERRA_MODEL, ProviderAdapter, ProviderResponse


INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "quality_floor": {
            "type": ["string", "null"],
            "enum": ["draft", "standard", "high", "critical", None],
        },
        "deadline_mode": {
            "type": "string",
            "enum": ["interactive", "absolute", "unspecified"],
        },
        "deadline_at": {"type": ["string", "null"], "format": "date-time"},
        "cost_cap_usd": {"type": ["number", "null"], "minimum": 0},
        "attention_profile": {
            "type": ["string", "null"],
            "enum": ["ask-freely", "batch-questions", "do-not-interrupt", None],
        },
    },
    "required": [
        "quality_floor",
        "deadline_mode",
        "deadline_at",
        "cost_cap_usd",
        "attention_profile",
    ],
}


class ParsedIntent(BaseModel):
    """Validated output of the one focused LLM extraction call."""

    model_config = ConfigDict(extra="forbid")

    quality_floor: QualityFloor | None = None
    deadline_mode: Literal["interactive", "absolute", "unspecified"]
    deadline_at: datetime | None = None
    cost_cap_usd: float | None = Field(default=None, ge=0)
    attention_profile: AttentionProfile | None = None


class IntentExtractor:
    def __init__(
        self,
        provider: ProviderAdapter,
        *,
        model: str = LUNA_MODEL,
        fallback_model: str | None = TERRA_MODEL,
        default_deadline: timedelta = timedelta(hours=24),
        default_cost_cap_usd: float = 2.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.fallback_model = fallback_model
        self.default_deadline = default_deadline
        self.default_cost_cap_usd = default_cost_cap_usd
        self.last_provider_response: ProviderResponse | None = None
        self.provider_responses: list[ProviderResponse] = []
        self.fallback_events: list[dict[str, str]] = []

    async def extract(
        self,
        request_text: str,
        *,
        explicit: Mapping[str, Any] | Any | None = None,
        now: datetime | None = None,
    ) -> Any:
        """Return a ``TaskContract`` with provenance for every field.

        Explicit values override parsed values. A missing parsed value receives
        a deterministic default. ``explicit`` may be a mapping or a Pydantic
        request object, which keeps the extractor independent of the API layer.
        """

        now = _aware(now or datetime.now(timezone.utc))
        prompt = _intent_prompt(request_text, now)
        self.provider_responses = []
        self.fallback_events = []
        response = await self.provider.complete(
            prompt,
            model=self.model,
            reasoning_effort="low",
            json_schema=INTENT_SCHEMA,
            schema_name="intent_contract",
            role="intent_extraction",
        )
        self.provider_responses.append(response)
        try:
            parsed = ParsedIntent.model_validate_json(response.text)
        except ValidationError as exc:
            if self.fallback_model is None or self.fallback_model == self.model:
                raise ValueError(f"Provider returned an invalid intent contract: {exc}") from exc
            self.fallback_events.append(
                {
                    "role": "intent_extraction",
                    "from_model": self.model,
                    "to_model": self.fallback_model,
                    "reason": "schema-validation-failure",
                }
            )
            response = await self.provider.complete(
                prompt,
                model=self.fallback_model,
                reasoning_effort="low",
                json_schema=INTENT_SCHEMA,
                schema_name="intent_contract",
                role="intent_extraction",
            )
            self.provider_responses.append(response)
            try:
                parsed = ParsedIntent.model_validate_json(response.text)
            except ValidationError as fallback_exc:
                raise ValueError(
                    f"Provider fallback returned an invalid intent contract: {fallback_exc}"
                ) from fallback_exc
        self.last_provider_response = response

        supplied = _as_mapping(explicit)
        contract_values: dict[str, Any] = {}
        provenance: dict[str, str] = {}

        _resolve(contract_values, provenance, "quality_floor", supplied, parsed.quality_floor, "standard")
        _resolve(
            contract_values,
            provenance,
            "max_cost_usd",
            supplied,
            parsed.cost_cap_usd,
            self.default_cost_cap_usd,
            aliases=("cost_cap_usd",),
        )
        _resolve(
            contract_values,
            provenance,
            "attention_profile",
            supplied,
            parsed.attention_profile,
            "batch-questions",
        )

        explicit_interactive = supplied.get("interactive")
        explicit_deadline = supplied.get("deadline") or supplied.get("deadline_at")
        if explicit_interactive is not None:
            interactive = bool(explicit_interactive)
            provenance["interactive"] = "explicit"
        else:
            interactive = parsed.deadline_mode == "interactive"
            provenance["interactive"] = "parsed" if interactive else "default"

        if explicit_deadline is not None:
            deadline = _parse_datetime(explicit_deadline)
            provenance["deadline"] = "explicit"
        elif parsed.deadline_mode == "absolute" and parsed.deadline_at is not None:
            deadline = _aware(parsed.deadline_at)
            provenance["deadline"] = "parsed"
        elif interactive:
            deadline = now + timedelta(minutes=5)
            provenance["deadline"] = "parsed"
        else:
            deadline = now + self.default_deadline
            provenance["deadline"] = "default"

        contract_values.update(
            deadline=deadline,
            interactive=interactive,
            provenance=provenance,
        )
        from .models import TaskContract

        return TaskContract(**contract_values)


def _intent_prompt(request_text: str, now: datetime) -> str:
    return f"""Extract only scheduling intent from the user request.
Current UTC time is {now.isoformat()}.
Use interactive only when a person is waiting now. Use absolute only when the
request states a resolvable time. Do not invent a budget. Treat rough/quick as
draft quality, careful/thorough as high, and safety- or mission-critical work
as critical. Attention describes interruption tolerance, not desired quality.
Return only JSON matching the supplied schema.

USER REQUEST:
{request_text}"""


def _resolve(
    output: dict[str, Any],
    provenance: dict[str, str],
    field: str,
    explicit: Mapping[str, Any],
    parsed: Any,
    default: Any,
    *,
    aliases: tuple[str, ...] = (),
) -> None:
    keys = (field, *aliases)
    explicit_value = next((explicit[key] for key in keys if explicit.get(key) is not None), None)
    if explicit_value is not None:
        output[field] = explicit_value
        provenance[field] = "explicit"
    elif parsed is not None:
        output[field] = parsed
        provenance[field] = "parsed"
    else:
        output[field] = default
        provenance[field] = "default"


def _as_mapping(value: Mapping[str, Any] | Any | None) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return vars(value)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
