"""YamlKnowledgeProvider：从 yaml 文件加载案例 + 关键词 retrieve + 压缩渲染。

案例文件格式（每文件一个 list）：
    - id: ECS-BURST-001
      title: "..."
      version: {entities: ">=1.4,<1.5"}
      triggers: [Burst, BC1064, ISystem]
      anti_triggers: [brain, memory]   # 可选：任一命中则不召回（跨域同词降噪，ISS-006）
      category: ecs_perf
      bad_pattern: "..."
      recommended_pattern: "..."
      source: "MEMORY.md#..."   # 给人追溯，不进 prompt
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .base import Case, version_matches

logger = logging.getLogger("brainregion.knowledge.yaml")


def extract_keyword_hits(text: str, cases: list[Case]) -> set[str]:
    """triggers 词库（所有 case 的 triggers 去重）中，哪些在 text 出现（大小写不敏感）。"""
    triggers = {t for c in cases for t in c.triggers if isinstance(t, str) and t}
    low = (text or "").lower()
    return {t for t in triggers if t.lower() in low}


def render_for_prompt(cases: list[Case]) -> str:
    """压缩渲染：只 title/bad_pattern/recommended_pattern/category + id（丢 source/history，省 token）。"""
    if not cases:
        return "(无命中的历史踩坑案例。)"
    lines: list[str] = []
    for c in cases:
        lines.append(f"- [{c.id}] ({c.category}) {c.title}")
        lines.append(f"  反模式: {c.bad_pattern}")
        lines.append(f"  正解: {c.recommended_pattern}")
    return "\n".join(lines)


class YamlKnowledgeProvider:
    """从一个或多个目录加载 *.yaml 案例（每文件一个 list）。

    支持叠加（overlay）：传多个目录时，后出现的覆盖先出现的（同 id 取后者）。典型用法：
    framework 通用知识库目录 + 项目本地 `.brain-region/knowledge/` 或 legacy `.design-review/knowledge/` 目录。项目本地放
    敏感/项目特定案例（如自家网络同步设计），不随开源 framework 上传。
    """

    def __init__(self, knowledge_dir: "str | Path | list[str | Path]"):
        if isinstance(knowledge_dir, (str, Path)):
            self.dirs = [Path(knowledge_dir)]
        else:
            self.dirs = [Path(d) for d in knowledge_dir]
        self._cases: list[Case] = self._load()

    @property
    def dir(self) -> Path:
        """首个目录（向后兼容：老代码读 provider.dir）。"""
        return self.dirs[0] if self.dirs else Path()

    def _load(self) -> list[Case]:
        by_id: dict[str, Case] = {}
        order: list[str] = []
        for d in self.dirs:
            if not d.exists():
                continue
            n_before = len(by_id)
            for p in sorted(d.glob("*.yaml")):
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
                except Exception as e:  # noqa: BLE001
                    logger.warning("knowledge 文件解析失败 %s: %s", p, e)
                    continue
                if not isinstance(data, list):
                    logger.warning("knowledge 文件非 list %s，跳过", p)
                    continue
                for item in data:
                    c = Case(
                        id=item.get("id", ""),
                        title=item.get("title", ""),
                        triggers=[t for t in (item.get("triggers") or []) if isinstance(t, str)],
                        anti_triggers=[t for t in (item.get("anti_triggers") or []) if isinstance(t, str)],
                        category=item.get("category", ""),
                        bad_pattern=item.get("bad_pattern", ""),
                        recommended_pattern=item.get("recommended_pattern", ""),
                        version=dict(item.get("version") or {}),
                        source=item.get("source", ""),
                    )
                    if c.id and c.id not in by_id:
                        order.append(c.id)
                    if c.id:
                        by_id[c.id] = c  # 后者覆盖（overlay 语义）
            added = len(by_id) - n_before
            logger.info("knowledge 目录 %s：+新增 %d 条（累计唯一 %d）", d, added, len(by_id))
        cases = [by_id[i] for i in order if i in by_id]
        logger.info("knowledge 加载完成：%d 条唯一案例 from %d 目录", len(cases), len(self.dirs))
        return cases

    def list_cases(self) -> list[Case]:
        return list(self._cases)

    def add_case(self, case: Case) -> None:
        self._cases.append(case)

    def retrieve(
        self, text: str, project_version: dict[str, str] | None = None, top_k: int = 5
    ) -> list[Case]:
        pv = project_version or {}
        candidates = [c for c in self._cases if version_matches(c.version, pv)]
        low = (text or "").lower()
        hits = extract_keyword_hits(text, candidates)
        scored = [
            (c, len({t for t in c.triggers if t in hits})) for c in candidates
        ]
        # anti_triggers：任一负触发词命中 → 判定跨域同词误命中（如 game "dormant" 撞 brain "dormant"），不召回
        scored = [
            (c, s)
            for c, s in scored
            if s > 0
            and not any(isinstance(t, str) and t and t.lower() in low for t in c.anti_triggers)
        ]
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return [c for c, _ in scored[:top_k]]
