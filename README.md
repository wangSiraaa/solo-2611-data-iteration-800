# 独立源段目录 + 多语言交付仓 + 术语册与地区用字版

Python 3.11，SQLite。**两侧物理分离**：源段目录（含术语册）与交付仓各自持有
SQLite 数据库；交付侧通过只读连接（`mode=ro`）读取源目录**已发布的源产品**与
**已发布的术语册版本**，不把位置编号当身份，也不缓存源状态。文本全部为合成内容，
不做自动翻译、词义识别或句子生成。

## 快速运行

```bash
# 端到端跑完 materials/753 全部场景（含用只读客户端读新旧包）
python3 run_demo.py

# 地区用字版端到端场景（本文下方“地区版例子”的全部数字）
python3 -m delivery_system demo-regional

# 仅验证真实包文件
python3 materials/753/package_reader.py data/packages/PACK1.json

# 测试（标准库 unittest，无需 pytest）
python3 -m unittest discover -s tests -v
```

## 目录结构

```
delivery_system/
  models.py              不可变值对象：SourceSegment/SourceProduct/TranslationEntry/...
  source_catalog.py      源段目录（独立 SQLite + 发布快照 + release 历史）
                         + PublishedSourceReader（交付侧只读句柄）
  terminology.py         术语册：概念/章节树/拼写绑定/段归属/人工标记 + 不可变版本
                         + PublishedTerminologyReader（交付侧只读合同）
  coverage.py            条目评估 + 精确覆盖区间 DP + 失败诊断
  regional.py            地区版纯求解器：替稿可用性、概念一致性 DP、互斥约束核
  delivery_repository.py 译文条目、覆盖计划、不可变发布包、地区替稿/锁定/地区版
  package_io.py          translation-package/1 与 regional-edition/1 写入与校验
  cli.py / __main__.py   命令行
  demo.py                materials/753 端到端复现
  demo_regional.py       地区用字版端到端复现
tests/                   57 个测试：覆盖、过期、重组、不可变、规模、术语解析、地区决策
```

## 源段目录与交付仓（既有能力，未变）

- `segment_id` 是身份；`position` 只是当前序位。每段独立 `revision`，旧修订
  永不改标签。每次结构性变更写一条 `source_releases` 快照。
- 译文条目精确存储 `(segment_id, revision)`；只有 `current_usable` 可入选。
- `plan_cover` 在区间上做可达性 DP，拒绝“并集覆盖”假象；失败给出
  `no_current_cover` / `no_exact_cover` / `empty_source` 诊断。
- 发布包 `translation-package/1` 不可变；旧包与旧读者（`package_reader.py`）
  保持可用。

## 术语册（源目录新增，人工标引）

- **概念**（concept）是稳定身份；**章**有上级章，构成树。
- **绑定**：同一章内同一拼写最多绑定一个概念（`set_binding` 为替换语义）；
  同一概念在不同章可有不同原词；同一拼写在不同章可指不同概念。
- **解析**：标记的概念 = 从该段所属章**向上取最近绑定**（子章覆盖、父章继承）。
- **标记**（marker）人工标引 `(segment_id, segment_revision, spelling)`，
  身份跨术语册版本稳定；地区替稿按标记身份声明用字。
- **版本**：`term-publish` 把章节树、段所属章、标记所在段修订及拼写冻结进
  同一个可复核的术语册版本。版本独立于源段修订与译文字节——移动一段到另一章
  只出新术语册版本，原文与译块字节不变；旧版本永远可读。
- **禁止**（发布时校验，拒绝入库）：章节环、悬空段（文档段无章归属，或归属/
  标记指向不存在的对象）、无定义标记（拼写自所属章向上无任何绑定）。
- 交付仓通过 `PublishedTerminologyReader`（`mode=ro`，每次调用重读）取得
  每个标记的概念身份；地区编辑不直接读写源库。

## 地区用字版（交付仓新增）

- **派生**：地区版只从**实际已发布**的包派生（`base_package_id` 必须存在）。
- **替稿**：对已选译块，地区编辑给出有限套整句替稿：完整文字 + 该块每个标记
  的用字编号（按标记身份，恰好一次）+ 非负改稿成本。同一概念在块内被赋不同值
  的替稿不可用；标记集合不符、用字越界（1..3）的替稿同样剔除并记录原因。
- **锁定**：地区可预先锁定部分概念的用字（可改、可删）。
- **求解**：每块选一套，同一概念跨块取值一致，全书总成本最小；并列时取按包内
  块序比较字典序最小的替稿编号序列。无标记的块保留原文、成本 0。规模上限：
  至多 12 个实际概念、每个最多 3 种用字（超出报错）。
- **不可行**：给出不可约的互斥约束核（锁 + 块的候选集合；去掉任一成员即可行），
  不写出半本书——`regional_editions` 不落行、磁盘不落文件。
- **冻结**：`regional-edition/1` 记录实际文字、所选编号、总成本、概念取值及
  **对应源术语版本**；同 id 不同内容拒绝（不可变）。重开库后按指定版本重解，
  术语解析与地区决策逐字节重现（`content_sha256` 相同）。

## 地区版例子（demo-regional 实际跑出）

两个块都含概念 P：块一 P=甲(0)/乙(2)，块二 P=甲(5)/乙(0)。

| 场景 | 结果 |
|---|---|
| 无锁（术语册 V1） | 两处取乙，总成本 **2** |
| 锁 P=甲（V1） | 总成本 **5** |
| S2 移到“port→Q”的章并发布 V2，报价与锁不动 | 块一取甲、块二取乙，总成本 **0**，概念取值 {P:1, Q:2} |

另演示：一句整句同时约束概念 P 与 R（C1/C2 各自绑定两者），每套局部报价都是 0，
但全局无可行译本——求解器指出互斥约束核 {T_C, T_D} 且不产生任何地区版文件。
普通包 PACK 仍被 `package_reader.py` 正常读取。

## 常用命令

```bash
# 术语册（源侧）
delivery term-concept-add P --label "电气端口"
delivery term-chapter-add ELEC --parent ROOT
delivery term-bind ELEC port P          # 章内拼写→概念（替换语义）
delivery term-assign S1 ELEC            # 段归属（移动段即改归属）
delivery term-marker-add M1 S1 1 port   # 人工标记：段、段修订、拼写
delivery term-publish                   # 冻结新术语册版本
delivery term-show --version 1          # 只读复核某一版本

# 地区版（交付侧，源库只读）
delivery regional-quote-add TW T_A A1 --text "整句…" --assignments '{"M1":1}' --cost 0
delivery regional-lock-set TW P 1
delivery regional-solve TW PK_A --terminology-version 1 [--save-as ED1]
delivery regional-publish TW PK_A ED1 --terminology-version 1
delivery regional-edition-show ED1
```

## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP；地区求解是
按块序的状态 DP（状态仅为“后续块仍涉及的概念”的部分赋值），足以支撑
12 概念 × 3 用字的题目规模。未引入翻译、词义识别、自动造句、账号等平台能力。
