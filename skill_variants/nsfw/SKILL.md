---
name: nai-prompting
description: Write, check and repair NovelAI (NAI Diffusion V4.5 / V5) image prompts for the AstrBot NAI plugin — base prompt, character prompt boxes, Undesired Content, quality-tag and UC-preset choice, artist strings with numeric weights, multi-panel comic pages, in-image text, and plugin tool integration (NSFW edition enabled). Use whenever the user wants a prompt for NovelAI / NAI / V5 / V4.5, asks how to write NAI weights or artist strings (画师串), wants to test whether NAI can draw something, or pastes an NAI prompt that renders wrong — NAI 提示词 / 画师串 / 权重 / 角色框 / 分镜 / NAI 画不出来.
---

# NovelAI 提示词（NSFW 完整版）

用于给 NovelAI V4.5 / V5（含本插件直连站、第三方 `/ai/` 官方协议中转站、测试面板与聊天工具）编写、检查和修复提示词。
完整规则在 `references/rules.md`；实测过的配方在 `references/recipes.md`。插件内置 Skill 目录为只读，**不要尝试修改或追加内容到 `references/` 文件**。

## 先判断本次要做什么

1. **用户只要提示词 / 帮改提示词**（“帮我写一组 NAI 提示词”“这段为什么画歪了”）：只输出可直接复制到测试面板或 NAI 网页的提示词与参数建议，**不要自行调用 `NAI_Generate_Image` 出图**。
2. **用户明确要求出图**（“画一张……”“用我刚刚改好的再画”）：若当前环境启用了 `NAI_Generate_Image` 工具，直接按第 6 步规则传参调用；未启用工具时输出提示词并提示使用 `/image` 指令或测试面板。

## 流程

先把需求拆清楚：画面里要看到哪几样东西（验收清单）、谁在哪、朝哪边、谁对谁做什么，难点是哪一样。
画不对时先改写法，不要先调参数（见 `references/rules.md` 第九节）。

### 1. 先定四件事（用户没说就按默认）

| 项 | 默认建议 | 插件适配说明 |
| --- | --- | --- |
| 模型 | V5 Full（`nai-diffusion-5-full`） | 露骨内容**必须** Full；Curated 画不出乳头和下身。若使用 **Vibe 参考**需改选 **V4.5 Full**；**精准参考（director）与图生图 V4.5 / V5 均支持** |
| 尺寸 | 竖 `832x1216`；横 `1216x832`；方 `1024x1024` | 第三方 `/ai/` 协议接口直接使用 `宽x高`（64 倍数，如 `832x1216` / `1024x1536`）；直连模式使用分档名（`竖图` / `横图` / `方图` / `2K竖图` 等） |
| 步数 | `28` | 插件支持 `1`~`50` 步；实测 23 和 28 构图几乎一样，结构在前几步已定型，画不对优先改写法而非盲目拉高步数 |
| 引导 / CFG Rescale | `8` / `0.1` | 同种子对照：5/0 和 8/0.1 差别不大；结构复杂时 `scale=8` 命中率明显高于 `5`。用画师串时按画师串作者给的参数 |
| 一张图几个人 | 1 | 2 人以上用角色框或插件 `char1_prompt` / `char2_prompt`（见第 3 步） |

再判断这张图属于哪种：单人 / 多人 / 分镜页 / 带文字。分镜和带文字有专门设置（第 4 步）。

### 2. 写主提示词（Base Prompt）

顺序：**人数 → 角色名/作品名 → 外貌 → 服装 → 表情 → 场景背景 → 机位景别 → 动作 → 质量词**。

- **姿势、体位、机位、景别、视线用标签**：`squatting` `on all fours` `from behind` `from above` `full body` `looking back`。
- **人和人、人和物之间的关系用一句英文句子**：谁的手碰到谁的哪里、东西插在哪里、液体从哪里流到哪里。
- **露骨概念一律“标签 + 一句具体描述”**。实测：光写 `anal prolapse` 画不准，加一句写清形状、位置、质感才对。所有道具、体液、扩张、排出类都一样。
- **现成角色（模型认识的）只写名字**：`aemeath (wuthering waves)`。不要再列她的发色瞳色。原创角色才把外貌写全。
  名字写法照 NAI 联想习惯：下划线换空格，括号照原样（`kisaki (blue archive)`、`hatsune miku`）。联想只是参考，查不到的名字照样能写，见 `references/rules.md` 第十一节第 2、3 条。
- **用户用中文强调的地方才加权**：突出 / 强调 → `1.2::词::`；非常 / 最 / 一定要 → `1.5::词::`；淡化 / 稍微 → `0.8::词::`；“不要”写 `-1::词::` 或进负面，不是减权重。没强调的不加。
- **默认只用角色本名标签，不加换装/皮肤标签**（如 `kisaki (swimsuit) (blue archive)`）。皮肤标签会把整张图拉向官方原画；要换衣服就用本名标签 + 写出衣服（属性写全：款式、颜色、材质、透明度）。只有用户点名要某个官方皮肤时才加皮肤标签。
- **泳装、比基尼会把胸画大**（训练图偏向）。现成角色在这类场景要按原设写明胸部大小（B 罩杯 = `small breasts`），负面压 `large breasts, huge breasts, cleavage`。
- **互斥组每组只选一个**（视线 / 景别 / 背景 / 水平机位 / 垂直机位），见 `references/rules.md` 第二节。
- **有容易挡住重点的东西要先处理**。比如有尾巴的角色从后面拍要写 `tail raised, tail lifted`，否则尾巴挡住。

### 2.5 写足细节（按需展开九层）

用户希望画面精细完整时，主提示词按下面九层写具体（不许用一个大路货词带过）；**若是局部微调或用户要求忠实短句，只改指定部分，不要擅自堆砌无关背景**：

| 层 | 要写到什么程度 | 反例（太空） |
| --- | --- | --- |
| 画风 | **注意与插件画风槽位区分**：若直接调用 `NAI_Generate_Image` 且插件开启了画风槽位/默认画风，不要再在 `prompt` 里硬塞画师串以免叠串；若是给用户复制到网页/测试面板，可放在最前或按槽位选择 | — |
| 角色 | 本名标签 + 场景会带偏的身材项（泳装写胸部大小） | — |
| 服装 | 款式、颜色、材质、透明度（不透明写 `opaque ...` 防止跟透视装串）、装饰、状态 | `swimsuit` |
| 表情 | 2~4 个：神态 + 嘴 + 脸红/眼神 | `smile` |
| 姿势动作 | 姿势标签 + 一句写清动作和它造成的效果 | `standing` |
| 场景 | 地点 + 2~3 个具体道具/地标 + 远景 + 时间天气 | `beach` |
| 光照 | 具名布光：光从哪来、什么颜色、打出什么效果（轮廓光、焦散、反光） | `sunlight` |
| 镜头 | 景别 + 机位 + 景深/前景虚化/透视 | 只有 `full body` |
| 色彩与氛围 | 主色调一句 + 粒子特效（水花、光斑、花瓣、雨丝） | 什么都不写 |

再加一两句自然语言把“画面最出彩的那一刻”写具体。
**清单里有难点时先保难点**（少见的姿势、方向关系、多人谁做什么）：九层写满会冲淡难点。先用最少的场景把难点画对，再一层层加回装饰，每加一层确认难点还在。
质量尾：开着 `Add Quality Tags` 时只追加它没有的（`best quality, absurdres, detailed background`）。

### 3. 多人图

- 主提示词放人数（`2girls` 或 `duo, two adult women`）、场景、画风、整体动作描述。
- 每人一个角色框，**只放这个人的外貌和表情**，末尾加互动前缀：`mutual#动作` / `source#动作` / `target#动作`。
- 两人的身体接触写进主提示词的一句话里，用名字或外观指明是谁。
- 用户的工具没有角色框时：把每个人的外貌写成一句接在主提示词后面（或用 `|` 分隔），开头带名字。
- 负面里不要放 `extra characters`；角色框负面别写一长串别人特征，只放真会串过去的单个特征。

### 4. 分镜页、文字与参考图

- 分镜：`comic, multiple panels, manga page layout, clean panel borders` + 自然语言 `This is a 5-panel comic page...`，每格前写位置词（`First panel (top, wide):` `Second panel (middle-left):`...）。五格最稳，四格偶尔裂成五格。
- **分镜页负面预设选「不加」**：Heavy/Light 预设带 `multiple views`，会压分镜。
- 带文字：正面加 `text, english text`；**关掉 `Add Quality Tags`**（它带 `no text`），手写 `very aesthetic, masterpiece` 到末尾。
- 气泡台词写在该格描述末尾：`A speech bubble says "..."`。台词越短越容易拼对。拟声词写日文（中文两字拟声第二个字几乎都错）。不要在台词前写 `Text:`（会被画进气泡）。
- 剖面 / X 光：负面加 `see-through body, x-ray on body, transparent skin, penis on stomach, 2 penises`，优先使用 `inset` 角上小圆框或局部窗口写法（见 `references/recipes.md`）。
- **参考图（第三方 `/ai/` 官方协议接口）**：
  - **精准参考（Precise Reference / director）**：**V4.5 与 V5 均支持**，适合跨场景保持角色或画风；V4.5 主权重 `0`~`1`（建议 `0.9`~`1.0`）、次级特征 `0.5`，**V5 主权重支持 `0`~`5`（默认 `1.0`），且次级特征固定为 `0`、提取度固定为 `1`**。
  - **氛围迁移（Vibe Transfer）**：**仅限 V4.5**（V5 会被拦截），适合参考画风/色调/质感；默认权重 `0.6`。
  - **图生图（img2img）**：V4.5 与 V5 均支持，只用第一张图作底图重绘。精确参考与 Vibe 不可在同一次请求混用。

### 5. 写负面（Undesired Content）

- 没有特别要排除的东西：用插件默认反向提示词或默认预设，不手写乱堆。
- 有要排除的：只写要排除的词，加上通用防崩词（`bad anatomy, bad hands, extra digits, fewer digits, watermark, signature, logo`）。
- **露骨图必须带成年守卫**：`loli, child, aged down, petite, flat chest`。正面写 `adult`，体型写成年体型。露骨提示词里不写 `girl`（`1girl`、`fox girl` 都不写）。
- 测某个概念时，把**容易混淆的近似概念**也放进负面（测脱肛就压扩张，测排蛋就压跳蛋和触手），才看得出模型是不是真懂。
- 要定向去掉某个东西，正文里写 `-1::词::` 往往比放负面更准。

### 6. 本插件调用 & 格式检查

- **调用 `NAI_Generate_Image` 前检查**：
  1. 如果插件启用了 `NAI_Search_Grimoire`（法典词库），可直接检索或使用 `【法典:词条名】`；
  2. 注意 `style` 参数：若工具暴露了槽位名枚举，选对应槽位名或留空复用用户默认槽位，**不要在 `prompt` 开头再贴第二套画师串**；
  3. 开启了 `char1_prompt` / `char2_prompt` 时分框写入，未开启时写入单 `prompt`；
  4. `size_cn` 在第三方 `/ai/` 协议下传 `"宽x高"`，直连模式传中文分档名（`竖图`/`横图`/`2K竖图` 等）。
- **跑格式检查脚本**：若当前环境开启了 Computer Use（可执行 Python/Shell），使用与本 `SKILL.md` 同级的 `scripts/lint_nai.py` 绝对路径检查一遍（不要用相对路径）：
  ```bash
  python "<本SKILL目录>/scripts/lint_nai.py" --prompt "主提示词" --negative "负面" --size 832x1216 --steps 28 --quality on --fix
  ```
  如果当前环境未开启 Computer Use，则手动核对：数字结尾的标签（含空格如 `year 2024::`、`artist:as109::`）必须加逗号写成 `, ::`；所有权重 `::` 是否成对闭合；无 ComfyUI 语法 `(tag:1.2)` 或 `\(`；`Text:` 放在末尾；露骨图带齐成年守卫且不含 `girl`；多人角色框不写人数标签。

### 7. 交付格式（仅输出提示词时）

按这个顺序给，每段放代码块方便复制：

1. 一两句话说明这张图怎么设计的、和上一版改了什么。
2. 设置：模型、尺寸、步数、`Add Quality Tags` 开/关、负面预设选哪个。
3. **Prompt**（多人或启用角色框时再分 **角色 1**、**角色 2**）
4. **Undesired Content**
5. 需要时给变体：只说“把哪一句换成什么”，不要整段重贴。
6. **怎么判断**：列 3~4 条看图要点，以及每种失败该怎么调。

**连续修改**（用户看完图说“再改改”）：先判断是 A 微调（只改他说的那一处，其余一个标签都不动）、B 换人保场景、还是 C 全新，规则见 `references/rules.md` 第十一节第 4 条。判断不出来就按 A，并说一句。

## 权重速查

| 写法 | 效果 |
| --- | --- |
| `1.3::词::` | 加强到 1.3 倍，`::` 结束；不写结尾会一直作用到末尾 |
| `0.6::词::` | 减弱 |
| `-1::词::` | 定向去掉 / 反转（V4.5+） |
| `{词}` / `[词]` | ×1.05 / ÷1.05，每层叠一次 |
| `0.7::artist:as109, ::` | 数字结尾的名字**必须加逗号** |

调一个词先试 1.3~1.5；默认不加权。圆括号在 NAI 里不是权重，`\\(` 不需要。
