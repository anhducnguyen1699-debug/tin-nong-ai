"""Gọi Claude, ghi chi phí từng lần gọi, và chặn khi vượt ngân sách tháng."""
import anthropic
from pydantic import BaseModel

from . import db


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    pass


# Những bước được phép chạy cả khi vượt ngân sách (rẻ và cần thiết để trang còn có tin)
ESSENTIAL_STEPS = {"classify", "cluster"}


class LLM:
    def __init__(self, settings: dict, conn, run_id: int | None = None, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.conn = conn
        self.run_id = run_id
        self.client = client or anthropic.Anthropic(max_retries=3)
        self.run_cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def cost_of(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self.settings["prices_per_million"].get(model)
        if not price:
            return 0.0
        return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000

    def check_budget(self, step: str) -> None:
        if step in ESSENTIAL_STEPS:
            return
        spent = db.month_cost(self.conn)
        limit = self.settings["budget"]["monthly_usd_limit"]
        if spent >= limit:
            raise BudgetExceeded(f"Đã chi {spent:.2f} USD trong tháng, vượt mức {limit} USD. Bỏ bước '{step}'.")

    def parse(self, step: str, system: str, user: str, output_format: type[BaseModel], max_tokens: int) -> BaseModel:
        """Gọi mô hình của bước `step`, bắt trả JSON theo khuôn `output_format`."""
        self.check_budget(step)
        model = self.settings["models"][step]
        effort = self.settings.get("effort", {}).get(step, "medium")
        response = self.client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            output_config={"effort": effort},
        )
        usage = response.usage
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        output_tokens = usage.output_tokens or 0
        cost = self.cost_of(model, input_tokens, output_tokens)
        self.run_cost += cost
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        db.record_llm_call(self.conn, self.run_id, step, model, input_tokens, output_tokens, cost)

        if response.stop_reason == "refusal":
            raise LLMError(f"Mô hình từ chối ở bước {step}")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Câu trả lời ở bước {step} bị cắt vì vượt max_tokens={max_tokens}")
        if response.parsed_output is None:
            raise LLMError(f"Không đọc được JSON ở bước {step}")
        return response.parsed_output


class HandoffPending(Exception):
    """Bước này đang chờ câu trả lời viết tay."""


class HandLLM:
    """Chạy thử không tốn API: ghi đề bài ra file, đọc câu trả lời viết tay, kiểm theo đúng khuôn JSON."""

    def __init__(self, folder):
        from pathlib import Path
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.run_cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def parse(self, step, system, user, output_format, max_tokens):
        import hashlib, json
        name = f"{step}-{hashlib.sha1(user.split("<bai_moi>")[-1].encode()).hexdigest()[:8]}"
        answer = self.folder / f"{name}.answer.json"
        if answer.exists():
            return output_format.model_validate_json(answer.read_text(encoding="utf-8"))
        (self.folder / f"{name}.json").write_text(json.dumps(
            {"step": step, "schema": output_format.model_json_schema(), "system": system, "user": user},
            ensure_ascii=False, indent=1), encoding="utf-8")
        (self.folder / "CURRENT").write_text(name, encoding="utf-8")  # để `python -m pipeline.hand` biết đề nào đang chờ
        raise HandoffPending(str(self.folder / f"{name}.json"))
