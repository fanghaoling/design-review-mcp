你是盲评评审。下面给出对【同一份待审查内容】的若干份候选审查输出，标签为 X / Y / Z（已打乱，你不知道哪个用了哪种方法）。

对每一份输出，独立打分，输出严格 JSON：
{"X": {"useful": int, "correct": int, "harmful": int, "missed_critical": int, "overall": int}, "Y": {...}, "Z": {...}}

字段含义：
- useful：有价值的建议条数（主指标；越多越好）
- correct：正确建议条数（正确 ≠ 被采纳；区分这两者）
- harmful：错误/有害/误导建议条数（越少越好）
- missed_critical：本应指出却遗漏的关键问题数（硬门槛方向；越少越好）
- overall：整体质量 1-5（5 最好）

可选额外字段（能填就填，填不出可省略，值为 0-1 浮点）：precision、recall、novelty、coverage、conflict、redundancy。

只看建议的实质内容（severity/title/evidence/suggestion），不要试图猜测输出来自哪种方法（检索开/关/随机都可能与内容无关）。各标签独立打分，不要互相参照偏移。
