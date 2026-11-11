"""Adapt rejected completion parameters without guessing customer deployment aliases."""
from __future__ import annotations

import logging
from typing import Any

from openai import BadRequestError

log = logging.getLogger("cloudlens.model")


async def create_completion(client: Any, **options: Any) -> Any:
    options = dict(options)
    adapted: set[str] = set()
    while True:
        try:
            return await client.chat.completions.create(**options)
        except BadRequestError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body)
            if not isinstance(error, dict):
                raise
            parameter = error.get("param")
            code = error.get("code")
            if parameter in adapted or code not in {"unsupported_value", "unsupported_parameter"}:
                raise
            if parameter == "temperature" and "temperature" in options:
                del options["temperature"]
            elif parameter == "max_tokens" and "max_tokens" in options \
                    and "max_completion_tokens" not in options:
                options["max_completion_tokens"] = options.pop("max_tokens")
            else:
                raise
            adapted.add(parameter)
            log.info("Model rejected %s; retrying with compatible completion parameters", parameter)
