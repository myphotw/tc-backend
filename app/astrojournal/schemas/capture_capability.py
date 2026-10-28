from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_CAPTURE_CAPABILITY_BYTES = 64 * 1024

PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]


class _CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class DiscreteCaptureExposureCapability(_CapabilityModel):
    type: Literal["discrete"]
    values_seconds: list[PositiveFloat] = Field(min_length=1)


class RangeCaptureExposureCapability(_CapabilityModel):
    type: Literal["range"]
    min_seconds: PositiveFloat
    max_seconds: PositiveFloat
    step_seconds: PositiveFloat

    @model_validator(mode="after")
    def validate_range(self) -> "RangeCaptureExposureCapability":
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be greater than or equal to min_seconds")
        return self


class FreeInputCaptureExposureCapability(_CapabilityModel):
    type: Literal["free_input_range"]
    min_seconds: PositiveFloat
    max_seconds: PositiveFloat

    @model_validator(mode="after")
    def validate_range(self) -> "FreeInputCaptureExposureCapability":
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be greater than or equal to min_seconds")
        return self


CaptureExposureCapability = Annotated[
    DiscreteCaptureExposureCapability
    | RangeCaptureExposureCapability
    | FreeInputCaptureExposureCapability,
    Field(discriminator="type"),
]


class CaptureModeCapability(_CapabilityModel):
    id: str
    type: Literal["altAz", "equatorial", "altAzDerotated", "custom"]
    label: str
    exposure_capability: CaptureExposureCapability | None = None

    @field_validator("id", "label")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mode id and label cannot be blank")
        return value


class DiscreteGainCapability(_CapabilityModel):
    type: Literal["discrete"]
    values: list[NonNegativeFloat] = Field(min_length=1)


class RangeGainCapability(_CapabilityModel):
    type: Literal["range"]
    min: NonNegativeFloat
    max: NonNegativeFloat
    step: PositiveFloat

    @model_validator(mode="after")
    def validate_range(self) -> "RangeGainCapability":
        if self.max < self.min:
            raise ValueError("gain max must be greater than or equal to min")
        return self


GainCapability = Annotated[
    DiscreteGainCapability | RangeGainCapability,
    Field(discriminator="type"),
]


class FilterCapability(_CapabilityModel):
    id: str
    type: Literal["off", "broadband", "dualBand", "narrowBand", "custom"]
    name: str

    @field_validator("id", "name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("filter id and name cannot be blank")
        return value


class ChannelCombinationCapability(_CapabilityModel):
    id: str
    name: str
    primary_label: str
    secondary_label: str
    ratio_adjustable: bool = False
    min_primary_percent: int = Field(default=0, ge=0, le=100)
    max_primary_percent: int = Field(default=100, ge=0, le=100)
    step_percent: int = Field(default=1, gt=0)

    @field_validator("id", "name", "primary_label", "secondary_label")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("channel-combination identifiers and labels cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "ChannelCombinationCapability":
        if self.max_primary_percent < self.min_primary_percent:
            raise ValueError(
                "max_primary_percent must be greater than or equal to min_primary_percent"
            )
        return self


class EquipmentCaptureCapability(_CapabilityModel):
    version: Literal[1]
    modes: list[CaptureModeCapability] = Field(default_factory=list)
    gain_capability: GainCapability | None = None
    filters: list[FilterCapability] = Field(default_factory=list)
    channel_combinations: list[ChannelCombinationCapability] = Field(
        default_factory=list
    )

    @model_validator(mode="before")
    @classmethod
    def validate_payload_size(cls, value: object) -> object:
        if isinstance(value, cls):
            return value
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("capture_capability must be finite JSON") from exc
        if len(encoded) > MAX_CAPTURE_CAPABILITY_BYTES:
            raise ValueError(
                f"capture_capability exceeds {MAX_CAPTURE_CAPABILITY_BYTES} bytes"
            )
        return value

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "EquipmentCaptureCapability":
        self._require_unique("mode", [item.id for item in self.modes])
        self._require_unique("filter", [item.id for item in self.filters])
        self._require_unique(
            "channel combination",
            [item.id for item in self.channel_combinations],
        )
        return self

    @staticmethod
    def _require_unique(kind: str, values: list[str]) -> None:
        if len(set(values)) != len(values):
            raise ValueError(f"{kind} ids must be unique")
