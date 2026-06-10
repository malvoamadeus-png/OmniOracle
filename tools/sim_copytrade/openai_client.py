from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests


def _load_env_file() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip().lstrip("\ufeff")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


def _first_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _extract_json_object(text: str) -> Dict[str, Any]:
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    stripped = text.strip()
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl > 0:
            stripped = stripped[first_nl + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
        text = stripped

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = text.find("{")
    if start < 0:
        raise ValueError("response did not contain a JSON object")

    depth = 0
    in_str = False
    escape = False
    end = -1
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end < 0:
        raise ValueError("failed to extract JSON object from response")
    obj = json.loads(text[start:end])
    if not isinstance(obj, dict):
        raise ValueError("extracted JSON was not an object")
    return obj


@dataclass
class OpenAIReportResult:
    parsed: Dict[str, Any]
    raw_text: str
    model: str
    request_id: Optional[str]
    usage: Dict[str, int]


class OpenAIAnalysisClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: float = 300.0,
        request_retries: int = 3,
        allow_nonstream_fallback: Optional[bool] = None,
    ) -> None:
        _load_env_file()
        self.api_key = (
            api_key
            or _first_env("OPENAI_API_KEY", "GPT_API_KEY")
        )
        if not self.api_key:
            raise ValueError("Missing API key. Set OPENAI_API_KEY or GPT_API_KEY.")
        self.base_url = (base_url or _first_env("OPENAI_BASE_URL", "GPT_BASE_URL", default="https://api.openai.com/v1")).rstrip("/")
        self.model = model or _first_env("OPENAI_MODEL_NAME", "OPENAI_RESEARCH_MODEL", "GPT_SUMMARY_MODEL", default="gpt-5.4")
        self.timeout_s = max(15.0, float(timeout_s))
        self.request_retries = max(1, int(request_retries))
        if allow_nonstream_fallback is None:
            fallback_raw = os.getenv("OPENAI_ALLOW_NONSTREAM_FALLBACK", "1").strip().lower()
            self.allow_nonstream_fallback = fallback_raw in {"1", "true", "yes", "on"}
        else:
            self.allow_nonstream_fallback = bool(allow_nonstream_fallback)

    def _candidate_models(self) -> List[str]:
        values: List[str] = []
        values.append(self.model)
        for key in ("OPENAI_MODEL_NAME", "OPENAI_RESEARCH_MODEL", "GPT_SUMMARY_MODEL"):
            v = os.getenv(key, "").strip()
            if v:
                values.append(v)
        fallback_raw = os.getenv("OPENAI_FALLBACK_MODELS", "gpt-5.4,gpt-4.1").strip()
        if fallback_raw:
            values.extend([v.strip() for v in fallback_raw.split(",") if v.strip()])
        dedup: List[str] = []
        for v in values:
            if v and v not in dedup:
                dedup.append(v)
        return dedup or ["gpt-5.4"]

    @staticmethod
    def _is_model_unavailable(status_code: int, err_text: str) -> bool:
        if status_code not in {400, 404, 422, 429, 500, 503}:
            return False
        msg = (err_text or "").lower()
        markers = (
            "model_not_found",
            "model is not found",
            "model does not exist",
            "unsupported model",
            "not supported",
            "no available channel for model",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _is_unsupported_param(err_text: str, param_name: str) -> bool:
        msg = (err_text or "").lower()
        pname = str(param_name or "").lower()
        if not pname:
            return False
        markers = (
            f"unknown parameter '{pname}'",
            f"unknown parameter: {pname}",
            f"unsupported parameter '{pname}'",
            f"unsupported parameter: {pname}",
            f"invalid field '{pname}'",
            f"invalid field: {pname}",
            f"additional properties are not allowed ('{pname}'",
            f"unrecognized request argument supplied: {pname}",
            f"invalid request: {pname}",
        )
        return any(marker in msg for marker in markers)

    def _chat_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 2600,
        temperature: float = 0.2,
        reasoning_effort: Optional[str] = None,
    ) -> OpenAIReportResult:
        def _parse_sse_body(raw_text: str) -> Optional[Dict[str, Any]]:
            text = str(raw_text or "")
            if "data:" not in text:
                return None

            chunks: List[str] = []
            events: List[Dict[str, Any]] = []
            usage: Dict[str, Any] = {}
            request_id: Optional[str] = None
            model_name: Optional[str] = None

            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload_text = line[len("data:") :].strip()
                if not payload_text or payload_text == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload_text)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                events.append(obj)

                if request_id is None and isinstance(obj.get("id"), str):
                    request_id = obj.get("id")
                if model_name is None and isinstance(obj.get("model"), str):
                    model_name = obj.get("model")
                if isinstance(obj.get("usage"), dict):
                    usage = obj.get("usage") or usage

                choices = obj.get("choices")
                if isinstance(choices, list) and choices:
                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                    message = choice0.get("message") if isinstance(choice0.get("message"), dict) else {}
                    delta = choice0.get("delta") if isinstance(choice0.get("delta"), dict) else {}

                    content_msg = message.get("content")
                    if isinstance(content_msg, str) and content_msg:
                        chunks.append(content_msg)
                    content_delta = delta.get("content")
                    if isinstance(content_delta, str) and content_delta:
                        chunks.append(content_delta)

            if not chunks and events:
                def _walk(node: Any) -> None:
                    if isinstance(node, dict):
                        for key, value in node.items():
                            k = str(key).lower()
                            if k in {"content", "output_text", "text"} and isinstance(value, str) and value:
                                chunks.append(value)
                            _walk(value)
                    elif isinstance(node, list):
                        for item in node:
                            _walk(item)

                for event in events:
                    _walk(event)

            content = "".join(chunks).strip()
            if not content:
                return None
            return {
                "id": request_id,
                "model": model_name,
                "usage": usage,
                "choices": [{"message": {"content": content}}],
            }

        def _parse_chat_response(resp: requests.Response) -> Dict[str, Any]:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

            sse_data = _parse_sse_body(resp.text or "")
            if isinstance(sse_data, dict):
                return sse_data

            snippet = (resp.text or "")[:500]
            raise RuntimeError(f"OpenAI API returned non-JSON response and SSE parse failed: {snippet}")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }

        last_error: Optional[str] = None

        def _is_retriable_status(code: int) -> bool:
            return code in {408, 409, 429, 500, 502, 503, 504}

        def _sleep_backoff(attempt_idx: int) -> None:
            delay = min(8.0, 0.8 * (2 ** max(0, attempt_idx - 1)))
            time.sleep(delay)

        def _call_once(req_payload: Dict[str, Any], *, use_stream: bool) -> OpenAIReportResult:
            call_payload = dict(req_payload)
            call_payload["stream"] = bool(use_stream)

            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=call_payload,
                timeout=self.timeout_s,
                stream=bool(use_stream),
            )
            if resp.status_code != 200:
                err = (resp.text or "")[:500]
                raise RuntimeError(f"HTTP {resp.status_code} {err}")

            resp.encoding = "utf-8"

            if use_stream:
                stream_lines: List[str] = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if raw_line is None:
                        continue
                    line = str(raw_line).strip()
                    if not line:
                        continue
                    stream_lines.append(line)
                stream_text = "\n".join(stream_lines).strip()
                if not stream_text:
                    raise RuntimeError("empty streaming response chunks")
                data = _parse_sse_body(stream_text)
                if not isinstance(data, dict):
                    try:
                        parsed = json.loads(stream_text)
                    except Exception:
                        parsed = None
                    if not isinstance(parsed, dict):
                        snippet = stream_text[:500]
                        raise RuntimeError(f"streaming response parse failed: {snippet}")
                    data = parsed
            else:
                data = _parse_chat_response(resp)
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("OpenAI API response missing choices")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("OpenAI API returned empty content")
            usage_raw = data.get("usage") if isinstance(data, dict) else {}
            usage = {
                "input_tokens": int((usage_raw or {}).get("prompt_tokens", 0) or 0),
                "output_tokens": int((usage_raw or {}).get("completion_tokens", 0) or 0),
            }
            request_id = data.get("id") if isinstance(data, dict) else None
            parsed = _extract_json_object(content)
            return OpenAIReportResult(
                parsed=parsed,
                raw_text=content,
                model=str(req_payload.get("model") or self.model),
                request_id=request_id if isinstance(request_id, str) else None,
                usage=usage,
            )

        for model_name in self._candidate_models():
            model_done = False
            attempts = [True, False] if reasoning_effort else [False]
            for try_reasoning in attempts:
                req_payload = dict(payload)
                req_payload["model"] = model_name
                if try_reasoning and reasoning_effort:
                    req_payload["reasoning_effort"] = str(reasoning_effort)
                stream_modes: List[bool] = [True]
                if self.allow_nonstream_fallback:
                    stream_modes.append(False)

                for use_stream in stream_modes:
                    for attempt_idx in range(1, self.request_retries + 1):
                        try:
                            result = _call_once(req_payload, use_stream=use_stream)
                            self.model = model_name
                            return result
                        except RuntimeError as exc:
                            err_text = str(exc)
                            status_code = None
                            if err_text.startswith("HTTP "):
                                try:
                                    status_code = int(err_text.split(" ", 2)[1])
                                except Exception:
                                    status_code = None

                            if (
                                try_reasoning
                                and reasoning_effort
                                and status_code in {400, 422}
                                and self._is_unsupported_param(err_text, "reasoning_effort")
                            ):
                                break

                            if status_code is not None and self._is_model_unavailable(status_code, err_text):
                                last_error = f"{model_name}: {err_text}"
                                model_done = True
                                break

                            last_error = f"{model_name}: {err_text}"
                            should_retry = (status_code is None or _is_retriable_status(status_code)) and (
                                attempt_idx < self.request_retries
                            )
                            if should_retry:
                                _sleep_backoff(attempt_idx)
                                continue
                        except requests.RequestException as exc:
                            last_error = f"{model_name}: {exc}"
                            if attempt_idx < self.request_retries:
                                _sleep_backoff(attempt_idx)
                                continue
                        break
                    if model_done:
                        break
                if model_done:
                    break

            if model_done:
                continue

        raise RuntimeError(f"No available model for OpenAI call. Last error: {last_error}")

    @staticmethod
    def _validate_required_keys(parsed: Dict[str, Any], required_keys: Sequence[str]) -> Tuple[bool, List[str]]:
        missing: List[str] = []
        for key in required_keys:
            if key not in parsed:
                missing.append(str(key))
        return (len(missing) == 0), missing

    def generate_copytrade_analysis(
        self,
        *,
        analysis_payload: Dict[str, Any],
        language: str = "zh-CN",
        required_keys: Optional[Sequence[str]] = None,
    ) -> OpenAIReportResult:
        keys = list(required_keys) if required_keys else [
            "purpose",
            "what_was_tested",
            "key_outcome",
            "gap_vs_leader",
            "root_causes",
            "action_plan",
            "next_run_commands",
            "caveats",
        ]
        keys_text = ", ".join(keys)
        system_prompt = (
            "You are a quantitative trading research writer and execution analyst. "
            "Return ONLY one JSON object. "
            "No markdown, no code fences, no prose outside JSON. "
            "Write in Chinese, objective and evidence-driven, with thesis-like narrative style."
        )
        user_prompt = (
            f"Language: {language}\n"
            "Task: summarize executed copytrade experiments for decision making.\n"
            "Output must be execution-oriented and evidence-based.\n"
            "Write paragraph-style narrative in objective/research tone.\n"
            "Do NOT dump raw JSON or parameter lists in the main conclusions.\n"
            "Do NOT use capital-scale mismatch / short-window / mirror-sell-count as main causes unless explicitly supported by thresholds.\n"
            "Must explain why optimal entries can be higher than average, and cite top-market evidence.\n"
            "For fields objective/what_was_executed/key_results/gap_to_leader/final_winner_reason: output concise paragraphs.\n"
            "Output JSON keys exactly:\n"
            f"{keys_text}\n\n"
            "Input data JSON:\n"
            f"{json.dumps(analysis_payload, ensure_ascii=False)}"
        )
        result = self._chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=5200,
            temperature=0.05,
            reasoning_effort="xhigh",
        )
        ok, missing = self._validate_required_keys(result.parsed, keys)
        if ok:
            return result

        retry_prompt = (
            f"Your previous JSON missed keys: {', '.join(missing)}.\n"
            "Retry now. Return ONE JSON object only.\n"
            f"Required keys exactly: {keys_text}.\n"
            "No markdown, no prose outside JSON."
        )
        retry = self._chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": result.raw_text},
                {"role": "user", "content": retry_prompt},
            ],
            max_tokens=5200,
            temperature=0.0,
            reasoning_effort="xhigh",
        )
        ok_retry, missing_retry = self._validate_required_keys(retry.parsed, keys)
        if not ok_retry:
            raise RuntimeError(f"OpenAI response missing required keys after retry: {', '.join(missing_retry)}")
        return retry
