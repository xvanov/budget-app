import os
import subprocess
import json
import shutil
import logging
import re

logger = logging.getLogger(__name__)

BUDGET_CONTEXT_PATH = os.environ.get("BUDGET_CONTEXT_PATH", "/data/budget_context.md")
OPCODE_MODEL = os.environ.get("OPCODE_MODEL", "opencode/qwen3.6-plus-free")

def read_budget_context() -> str:
    try:
        if os.path.exists(BUDGET_CONTEXT_PATH):
            with open(BUDGET_CONTEXT_PATH, "r") as f:
                return f.read()
    except Exception as e:
        logger.warning("Failed to read budget context: %s", e)
    return ""

def build_system_prompt(extra_context: str = "") -> str:
    context = read_budget_context()
    prompt = (
        "You are a personal finance categorization assistant. "
        "Categorize each transaction into the provided category tree. "
        "Respond with JSON: {category_path, confidence_score, categorization_reason}.\n\n"
    )
    if context:
        prompt += "## Budget Context (user's financial profile)\n" + context + "\n\n"
    if extra_context:
        prompt += extra_context
    return prompt

def log_outbound_payload(prompt: str, model: str) -> None:
    logger.info(
        "LLM call: model=%s, system_prompt_includes_context=%s, prompt_length=%d",
        model,
        "budget_context.md" in prompt.lower() or "budget context" in prompt.lower(),
        len(prompt)
    )

def clean_response(text: str) -> str:
    """Strip ANSI codes, markdown code blocks, and leading/trailing non-JSON content."""
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'^.*?```(?:json)?\s*', '', text, count=1, flags=re.DOTALL)
    text = re.sub(r'\s*```.*$', '', text, count=1, flags=re.DOTALL)
    return text.strip()

FALLBACK_SIGNALS = [
    "insufficient credits", "quota exceeded", "rate limit",
    "402", "401", "invalid api key", "billing issue",
    "model not found", "you've hit your limit",
]

def has_fallback_signal(text: str) -> bool:
    lower = text.lower()
    return any(sig in lower for sig in FALLBACK_SIGNALS)

def call_llm(prompt: str, *, strong: bool = False) -> str:
    timeout = 180 if strong else 90

    system_prompt = build_system_prompt()
    full_prompt = system_prompt + "\n\n" + prompt if system_prompt else prompt
    log_outbound_payload(full_prompt, "strong" if strong else "fast")

    opencode_path = shutil.which("opencode")
    if opencode_path and not strong:
        model = OPCODE_MODEL
        try:
            cmd = [opencode_path, "run", "-m", model, full_prompt]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            output = clean_response(result.stdout.strip())
            if result.returncode == 0 and output and not has_fallback_signal(result.stdout + result.stderr):
                return output
        except subprocess.TimeoutExpired:
            logger.warning("OpenCode call timed out, falling back")
        except Exception as e:
            logger.warning("OpenCode call failed: %s, falling back", e)

    claude_path = shutil.which("claude")
    if claude_path:
        try:
            cmd = [claude_path, "-p", full_prompt, "--output-format", "text"]
            if strong:
                cmd.extend(["--model", "claude-opus-4-7"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                output = result.stdout.strip()
                return output
            if has_fallback_signal(result.stderr):
                logger.warning("Claude unavailable (rate-limited or auth error): %s", result.stderr[:200])
        except subprocess.TimeoutExpired:
            logger.warning("Claude call timed out")
        except Exception as e:
            logger.warning("Claude call failed: %s", e)

    logger.warning("No LLM available, returning fallback response")
    return json.dumps({
        "category_path": "Uncategorized",
        "confidence_score": 0.0,
        "categorization_reason": "No LLM available",
    })
