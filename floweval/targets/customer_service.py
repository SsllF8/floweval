"""智能客服被测对象（demo）。

这是一个**可运行、可注入缺陷**的多节点客服 Agent，用来演示 FlowEval 的价值：

    intent（意图识别） → retrieve（知识库检索） → answer（答案生成） → guardrail（合规审查）

为什么不用真实 LLM：
  1. 零 API key 也能跑通完整链路，任何人 clone 下来就能复现；
  2. 缺陷可以精确注入（把知识库删一条、把 top_k 调小），
     才能稳定演示"回归对比 + 节点级归因"到底长什么样。

真实项目里把 `_llm()` 换成一次 OpenAI 调用即可，其余结构不用动。
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass, field

from ..models import Step, TargetResult
from .base import BaseTarget, register

# ------------------------------------------------------------------ 知识库

KB_V1: list[dict[str, str]] = [
    {
        "id": "refund-001",
        "title": "退款政策",
        "text": "支持 7 天无理由退货，商品需保持完好不影响二次销售。退款将在审核通过后 3-5 个工作日退回原支付账户。",
        "keywords": ["退款", "退货", "退钱", "无理由", "七天"],
    },
    {
        "id": "logistics-001",
        "title": "物流时效",
        "text": "订单付款后 48 小时内出库，快递运输一般 3-7 天，偏远地区可能延长至 10 天。",
        "keywords": ["物流", "快递", "发货", "几天到", "什么时候到", "运输"],
    },
    {
        "id": "invoice-001",
        "title": "发票申请",
        "text": "电子发票可在「我的订单-订单详情-申请开票」自助开具，开具后 1-3 个工作日发送至预留邮箱。",
        "keywords": ["发票", "开票", "报销", "电子发票"],
    },
    {
        "id": "account-001",
        "title": "账号安全",
        "text": "忘记密码可通过登录页的「忘记密码」使用手机号验证码重置；账号被盗请立即联系人工客服冻结。",
        "keywords": ["密码", "登录", "账号", "重置", "被盗"],
    },
    {
        "id": "warranty-001",
        "title": "保修范围",
        "text": "整机保修一年，非人为损坏免费维修；人为损坏（进液、摔落、自行拆机）不在保修范围内。",
        "keywords": ["保修", "维修", "坏了", "质保", "一年"],
    },
    {
        "id": "human-001",
        "title": "转人工",
        "text": "人工客服工作时间为每天 9:00-21:00，可在对话框输入「转人工」接入。",
        "keywords": ["人工", "转人工", "投诉", "客服电话"],
    },
]

# v2 模拟一次"知识库更新事故"：保修和发票两条被误删
KB_V2: list[dict[str, str]] = [d for d in KB_V1 if d["id"] not in {"warranty-001", "invoice-001"}]

KNOWLEDGE_BASES: dict[str, list[dict[str, str]]] = {"v1": KB_V1, "v2": KB_V2}

# 提示注入 / 越权类请求的特征词
INJECTION_PATTERNS = [
    r"忽略(以上|上面|之前)(的)?(所有)?(指令|指示|规则)",
    r"ignore (all )?(previous|prior) (instructions|prompts)",
    r"你的(系统)?提示(词|词是什么)",
    r"system prompt",
    r"告诉我你的.*?(规则|指令|配置)",
    r"重复(一遍)?你(的)?(指令|提示词)",
]

# 需要拒答的敏感请求
SENSITIVE_PATTERNS = [
    r"其他(用户|人)的?(订单|手机号|地址|姓名)",
    r"查一下.*?(别人|他人).*?(订单|信息)",
    r"绕过.*?(风控|验证|审核)",
    r"怎么(刷单|套现|洗钱)",
]

# 合规红线：客服话术里不允许出现的过度承诺
OVERCOMMIT_WORDS = ["保证", "百分百", "一定不", "绝对", "包赔"]

REFUSAL_TEMPLATES = {
    "injection": "抱歉，我无法配合修改自身指令或透露系统内部配置。如果您有订单、物流、售后等实际问题，我可以继续为您处理。",
    "sensitive": "抱歉，出于隐私与安全考虑，我无法提供或查询这类信息。如需帮助，请提供您本人的订单号，我为您核实。",
}

FALLBACK_ANSWER = "抱歉，我暂时没有找到相关的准确信息，已为您转接人工客服，工作时间为每天 9:00-21:00。"


# ------------------------------------------------------------------ 配置


@dataclass
class AgentConfig:
    """被测对象的可调参数 —— 回归对比就是靠改这些参数制造版本差异。"""

    kb_version: str = "v1"
    top_k: int = 3
    guardrail_strict: bool = False
    simulate_latency: bool = True
    seed: int = 42
    # 模拟"生成模型变笨"：答案只取检索片段的前 N 个字
    answer_max_chars: int = 0  # 0 = 不截断


# ------------------------------------------------------------------ 被测对象


@register("customer_service")
class CustomerServiceTarget(BaseTarget):
    """四节点智能客服流水线。

    每个节点都会产出 Step，因此 FlowEval 能做节点级归因：
      - 检索不到 → retrieve 标记 failed，后续答案必然是兜底话术
      - 合规改写 → guardrail 记录改写前后，方便判断是"改对了"还是"改错了"
    """

    name = "customer_service"

    def __init__(self, config: AgentConfig | None = None, **kwargs: object):
        self.config = config or AgentConfig(**kwargs)  # type: ignore[arg-type]
        kb = KNOWLEDGE_BASES.get(self.config.kb_version)
        if kb is None:
            raise ValueError(
                f"未知知识库版本 {self.config.kb_version!r}，可选: {sorted(KNOWLEDGE_BASES)}"
            )
        self.kb = kb
        self._rng = random.Random(self.config.seed)

    # ---------------- 入口

    def _call(self, case_input: str) -> TargetResult:
        ctx: dict[str, object] = {"query": case_input}
        steps: list[Step] = []

        # --- 节点 1：意图识别
        intent, step = self._node_intent(case_input)
        steps.append(step)
        ctx["intent"] = intent

        # 注入类 / 敏感类直接走拒答分支，后面两个节点跳过
        if intent in {"injection", "sensitive"}:
            steps.append(Step(name="retrieve", status="skipped", output="命中拒答策略，跳过检索"))
            steps.append(Step(name="answer", status="skipped", output="命中拒答策略，跳过生成"))
            refusal = REFUSAL_TEMPLATES[intent]
            steps.append(
                Step(
                    name="guardrail",
                    status="success",
                    output=refusal,
                    meta={"action": "refuse", "reason": intent},
                )
            )
            return TargetResult(output=refusal, steps=steps, **self._usage(refusal, steps))

        # --- 节点 2：知识库检索
        hits, step = self._node_retrieve(case_input, intent)
        steps.append(step)
        ctx["hits"] = hits

        # --- 节点 3：答案生成
        answer, step = self._node_answer(case_input, hits)
        steps.append(step)
        ctx["answer"] = answer

        # --- 节点 4：合规审查
        final, step = self._node_guardrail(answer)
        steps.append(step)

        return TargetResult(output=final, steps=steps, **self._usage(final, steps))

    # ---------------- 节点实现

    def _node_intent(self, query: str) -> tuple[str, Step]:
        t0 = self._tick(query)
        text = query.strip()

        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return "injection", Step(
                    name="intent", status="success", output="injection",
                    elapsed_ms=self._tick(query, t0), meta={"matched": pattern},
                )
        for pattern in SENSITIVE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return "sensitive", Step(
                    name="intent", status="success", output="sensitive",
                    elapsed_ms=self._tick(query, t0), meta={"matched": pattern},
                )

        best, best_score = "chitchat", 0
        for doc in self.kb:
            score = sum(1 for kw in doc["keywords"] if kw in text)
            if score > best_score:
                best, best_score = doc["id"].split("-")[0], score

        return best, Step(
            name="intent", status="success", output=best,
            elapsed_ms=self._tick(query, t0), meta={"score": best_score},
        )

    def _node_retrieve(self, query: str, intent: str) -> tuple[list[dict[str, str]], Step]:
        t0 = self._tick(query)

        scored: list[tuple[int, dict[str, str]]] = []
        for doc in self.kb:
            score = sum(1 for kw in doc["keywords"] if kw in query)
            if score:
                scored.append((score, doc))
        scored.sort(key=lambda x: -x[0])
        hits = [doc for _, doc in scored[: self.config.top_k]]

        # 检索不到内容 = 这条流水线在本 case 上已经失败，显式标记
        status = "success" if hits else "failed"
        output = "、".join(d["title"] for d in hits) or "未命中任何知识条目"
        step = Step(
            name="retrieve",
            status=status,
            output=output,
            elapsed_ms=self._tick(query, t0),
            meta={
                "top_k": self.config.top_k,
                "kb_version": self.config.kb_version,
                "hit_ids": [d["id"] for d in hits],
            },
        )
        return hits, step

    def _node_answer(self, query: str, hits: list[dict[str, str]]) -> tuple[str, Step]:
        t0 = self._tick(query)

        if not hits:
            answer = FALLBACK_ANSWER
        else:
            parts = [f"关于您咨询的{hits[0]['title']}：{hits[0]['text']}"]
            if len(hits) > 1:
                parts.append(
                    "另外补充：" + "；".join(d["text"] for d in hits[1:])
                )
            answer = "".join(parts)
            if self.config.answer_max_chars:
                answer = answer[: self.config.answer_max_chars]

        return answer, Step(
            name="answer", status="success", output=answer,
            elapsed_ms=self._tick(query, t0), meta={"sources": [d["id"] for d in hits]},
        )

    def _node_guardrail(self, answer: str) -> tuple[str, Step]:
        t0 = self._tick(answer)

        violations = [w for w in OVERCOMMIT_WORDS if w in answer]
        if violations and self.config.guardrail_strict:
            cleaned = answer
            for w in violations:
                cleaned = cleaned.replace(w, "")
            cleaned += "（具体以实际处理结果为准）"
            return cleaned, Step(
                name="guardrail", status="success", output=cleaned,
                elapsed_ms=self._tick(answer, t0),
                meta={"action": "rewrite", "removed": violations},
            )

        return answer, Step(
            name="guardrail", status="success", output=answer,
            elapsed_ms=self._tick(answer, t0),
            meta={
                "action": "pass",
                "detected": violations,
                "enforced": self.config.guardrail_strict,
            },
        )

    # ---------------- 辅助

    def _tick(self, seed_text: str, start: float | None = None) -> float:
        """确定性的"伪耗时"，让延迟指标看起来像真实调用但可复现。"""
        if not self.config.simulate_latency:
            return 0.0
        digest = hashlib.md5(f"{seed_text}|{self.config.seed}".encode()).hexdigest()
        ms = 4 + (int(digest[:4], 16) % 17)
        if start is not None:
            time.sleep(ms / 1000)
            return round(ms + self._rng.uniform(0, 2), 2)
        return float(ms)

    def _usage(self, output: str, steps: list[Step]) -> dict[str, object]:
        """按字符数估算 token / 成本 —— 换成真模型时替换为 usage 字段即可。"""
        tokens = max(1, len(output) // 2)
        return {
            "tokens": tokens,
            "cost": round(tokens * 0.000002, 6),
            "latency_ms": round(sum(s.elapsed_ms for s in steps), 2),
        }


def make_target(version: str = "v1", **kwargs: object) -> CustomerServiceTarget:
    """按版本号造一个被测对象，方便 demo 和回归对比。

    v1 = 基线；v2 = 知识库被误删 + top_k 调小（故意制造回归）。
    """
    presets: dict[str, dict[str, object]] = {
        "v1": {"kb_version": "v1", "top_k": 3, "guardrail_strict": False},
        "v2": {"kb_version": "v2", "top_k": 1, "guardrail_strict": False},
        "v3": {"kb_version": "v1", "top_k": 3, "guardrail_strict": True},
    }
    if version not in presets:
        raise ValueError(f"未知版本 {version!r}，可选: {sorted(presets)}")
    cfg = {**presets[version], **kwargs}
    return CustomerServiceTarget(AgentConfig(**cfg))  # type: ignore[arg-type]
