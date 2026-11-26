---
name: nai-prompting
description: Write, check and repair NovelAI (NAI Diffusion V4.5 / V5) image prompts for the AstrBot NAI plugin — base prompt, character prompt boxes, Undesired Content, quality-tag and UC-preset choice, artist strings with numeric weights, multi-panel comic pages, in-image text, and plugin tool integration. Use whenever the user wants an SFW prompt for NovelAI / NAI / V5 / V4.5, asks how to write NAI weights or artist strings (画师串), wants to test whether NAI can draw a scene, or pastes an NAI prompt that renders wrong — NAI 提示词 / 画师串 / 权重 / 角色框 / 分镜 / NAI 画不出来.
---

# NovelAI 提示词（SFW 全年龄版）

用于给 NovelAI V4.5 / V5（含本插件直连站、第三方 `/ai/` 官方协议中转站、测试面板与聊天工具）编写、检查和修复全年龄提示词。
完整规则在 `references/rules.md`；实测配方与构图技巧在 `references/recipes.md`。插件内置 Skill 目录为只读，**不要尝试修改或追加内容到 `references/` 文件**。

## 先判断本次要做什么

1. **用户只要提示词 / 帮改提示词**（“帮我写一组 NAI 提示词”“这段为什么画歪了”）：只输出可直接复制到测试面板或 NAI 网页的提示词与参数建议，**不要自行调用 `NAI_Generate_Image` 出图**。
2. **用户明确要求出图**（“画一张……”“用我刚刚改好的再画”）：若当前环境启用了 `NAI_Generate_Image` 工具，直接按第 6 步规则传参调用；未启用工具时输出提示词并提示使用 `/image` 指令或测试面板。

## 流程

先把需求拆清楚：画面里要看到哪几样东西（验收清单）、谁在哪、朝哪边、谁对谁做什么，难点是哪一样。
画不对时先改写法，不要先调参数（见 `references/rules.md` 第九节）。

### 1. 先定基础参数（用户没说就按默认）

| 项 | 默认建议 | 插件适配说明 |
| --- | --- | --- |
| 模型 | V5 Full（`nai-diffusion-5-full`） | 通用首选；若使用 **Vibe 参考** 需改选 **V4.5 Full**，**精准参考（director）和图生图 V4.5 与 V5 均支持** |
| 尺寸 | 竖构图 `832x1216`；横构图 `1216x832`；方图 `1024x1024` | 第三方 `/ai/` 协议接口直接使用 `宽x高`（64 倍数，如 `832x1216` / `1024x1536`）；直连模式使用分档名（`竖图` / `横图` / `方图` / `2K竖图` 等） |
| 步数 | `24`~`28` | 插件支持 `1`~`50` 步；通常 24~28 步已足够，结构已在前几步定型，调高主要补纹理 |
| 引导（scale）/ CFG Rescale | `6`~`8` / `0.1`~`1.0` | 同种子对照中结构复杂画面 `scale=8` 命中率高于 `5`；若使用特定画师串则按作者推荐参数 |
| 人数 | 1 | 2 人以上优先使用角色框或插件 `char1_prompt` / `char2_prompt`（见第 3 步） |

再判断这张图属于哪种：单人 / 多人 / 分镜页 / 带文字。分镜和带文字有专门设置（第 4 步）。

### 2. 写主提示词（Base Prompt）

顺序：**人数 → 角色名/作品名 → 外貌 → 服装 → 表情 → 场景背景 → 机位景别 → 动作 → 质量词**。

- **姿势、机位、景别、视线用标签**：`squatting` `kneeling` `from behind` `from above` `full body` `looking back`。
- **人和人、人和物之间的关系用一句英文句子**：写清谁的手碰到哪里、谁面向谁、物体相对位置。
- **现成角色（模型认识的）只写名字**：如 `aemeath (wuthering waves)`。不要重复列她的发色瞳色。原创角色才把外貌写全。
  名字写法照 NAI 联想习惯：下划线换空格，作品名括号照原样（`kisaki (blue archive)`、`hatsune miku`）。联想只是参考，查不到的名字照样可写。
- **用户用中文强调的地方才加权**：突出 / 强调 → `1.2::词::`；非常 / 最 / 一定要 → `1.5::词::`；淡化 / 稍微 → `0.8::词::`；“不要”写 `-1::词::` 或进负面，不是减权重。没强调的不加。
- **默认只用角色本名标签，不加官方换装/皮肤标签**（如少用 `角色名 (swimsuit) (...)`）：官方皮肤标签容易把画面整体拉回立绘构图。要换装就用本名标签 + 具体描述新衣服（款式、颜色、材质、透明度）。
- **互斥组每组只选一个**（视线 / 景别 / 背景 / 水平机位 / 垂直机位），见 `references/rules.md` 第二节。
- **单侧特征与左右对应**：异瞳、单侧发饰/印记不要写成“跟着某只眼睛走”，直接用 `her right ... / her left ...` 各写死一句。
- **有容易遮挡重点的部位先处理**：如带兽耳/长尾角色从后方或躺姿拍摄时写 `tail raised` 或具体说明头顶上方留白（横图防切耳朵写法见 `references/recipes.md`）。

### 2.5 写足细节（按需展开九层）

用户需求较简略、希望画面精致完整时，主提示词按下面九层写具体（不要用单个大路货词带过）；**若只是做局部微调，或用户明确要求严格忠实短句，则只改指定处，不要擅自加戏**：

| 层 | 要写到什么程度 | 反例（太空） |
| --- | --- | --- |
| 画风 | **注意与插件画风槽位区分**：若直接调用 `NAI_Generate_Image` 且插件开启了画风槽位/默认画风，不要再在 `prompt` 里硬塞画师串以免叠串；若是给用户复制到网页/测试面板，可放在最前或使用槽位 | — |
| 角色 | 本名标签，或原创角色完整外貌（发色、瞳色、发型、配饰） | — |
| 服装 | 款式、颜色、材质（需写明是否不透明，如 `opaque white cotton`）、装饰、动态状态 | `dress` |
| 表情 | 2~4 个：神态 + 嘴型 + 眼神/微表情 | `smile` |
| 姿势动作 | 姿势标签 + 一句写清动作与产生的结果 | `standing` |
| 场景 | 地点 + 2~3 个具体道具/地标 + 远景 + 时间天气 | `beach` |
| 光照 | 具名布光：光源方向、色温与光影效果（轮廓光、焦散、窗户光斑） | `sunlight` |
| 镜头 | 景别 + 机位 + 景深/前景虚化/透视 | 只有 `full body` |
| 色彩与氛围 | 主色调一句 + 粒子/环境细节（水花、光斑、花瓣、细雨） | 什么都不写 |

**清单里有难点时先保难点**（少见姿势、精准左右手关系、多人交互）：九层写满容易冲淡难点。先把难点加权并精简无关装饰，确认主体准确后再补场景氛围。
质量尾：确认插件或网页开着 `Add Quality Tags` 时，只追加它没有的（如 `best quality, absurdres, detailed background`），避免重复堆 `masterpiece, very aesthetic`。

### 3. 多人图

- 主提示词放人数（`2girls`、`1girl, 1boy`）、场景、画风、整体动作描述。
- 每人一个角色框，**只放这个人的外貌、服装和表情**，末尾加互动前缀：`mutual#动作` / `source#动作` / `target#动作`。
- 两人的互动接触写进主提示词的一句话里，用外貌或衣着指明谁对谁（如 `the girl in the beige coat hands the cup to the girl with headphones`）。
- 角色框负面**只写要防串过去的特定小元素**（如 `braid, cheek mark`），不要把另一人的全套外貌词塞进负面，以免误伤本人特征（如异瞳）。
- 不用角色框时：在单框里用 `|` 分隔或接在主提示词后，每人一段并写明名字/外貌。
- 负面里不要放 `extra characters`。

### 4. 分镜页、文字与参考图

- **分镜**：`comic, multiple panels, manga page layout, clean panel borders` + 自然语言 `This is a 5-panel comic page...`，每格前写位置词（`First panel (top, wide):` `Second panel (middle-left):`...），加上 `The same <外观> character appears in every panel.`。五格通常最稳。
- **分镜页负面预设选「不加」**：Heavy/Light 预设带 `multiple views`，会压制分镜网格。
- **带文字**：正面加 `text, english text`（中文加 `chinese text`）；**关掉 `Add Quality Tags`**（自带 `no text`），改为手写 `very aesthetic, masterpiece` 到末尾。
- **气泡台词**：写在该格描述末尾：`A speech bubble says "..."`，台词越短越准。普通叙述句不要整段加英文双引号；也不要在台词前写 `Text:`，改放在整段提示词最后一行独立使用。
- **局部特写小圆框**：需要补充细节特写时，在对应格末尾加 `A small circular inset in the corner of this panel shows a close-up of ...`，并在开头加 `inset` 标签。
- **参考图（第三方 `/ai/` 官方协议接口）**：
  - **精准参考（Precise Reference / director）**：**V4.5 与 V5 均支持**，适合跨场景保持角色一致性或迁移画风；V4.5 主权重 `0`~`1`（建议 `0.9`~`1.0`）、次级特征权重 `0.5`，**V5 主权重支持 `0`~`5`（默认 `1.0`），且次级特征固定为 `0`、提取度固定为 `1`**。
  - **氛围迁移（Vibe Transfer）**：**仅限 V4.5**（V5 会被拦截），适合参考色调、光影与氛围，默认权重 `0.6`。
  - **图生图（img2img）**：V4.5 与 V5 均支持，以第一张参考图为底图重绘。两类参考（精准参考与 Vibe）不可同次混用。

### 5. 写负面（Undesired Content）

- 没有特别要排除的东西：使用插件默认反向提示词或官网预设，不乱堆词。
- 有特定要排除的：只写明确要避开的词 + 通用防崩词（`bad anatomy, bad hands, extra digits, fewer digits, watermark, signature, logo`）。
- 定向移除画面中某个元素或反转概念时，优先在正文里用 `-1::词::`（V4.5+），比直接丢进负面更准。

### 6. 本插件调用 & 格式检查

- **调用 `NAI_Generate_Image` 前检查**：
  1. 如果插件启用了 `NAI_Search_Grimoire`（法典词库），在需要特定画风、精致穿搭或复杂构图时，可先检索法典或使用 `【法典:词条名】`；
  2. 注意 `style` 参数：若工具 schema 提供了槽位画风列表，选对应槽位名或留空复用用户默认槽位，**不要同时在 `prompt` 开头再贴一组画师串**；
  3. 若开启了 `char1_prompt` / `char2_prompt` 参数，按第 3 步将主场景放 `prompt`、单人外貌分别放入 `char1_prompt` / `char2_prompt`；未暴露这两个参数时，合并写入单 `prompt`；
  4. `size_cn` 参数：第三方 `/ai/` 协议接口传 `"宽x高"`（64 倍数），直连模式传中文分档名（`竖图`/`横图`/`方图`/`2K竖图` 等）。
- **跑格式检查脚本**：如果当前运行环境支持 Python/Shell（已开启 Computer Use），使用与本 `SKILL.md` 同级的 `scripts/lint_nai.py` 绝对路径执行检查（不要使用相对路径，因为工作目录通常是 session workspace）：
  ```bash
  python "<本SKILL目录>/scripts/lint_nai.py" --prompt "主提示词" --negative "负面" --size 832x1216 --steps 28 --quality on --fix
  ```
  若当前环境未开启 Computer Use（无法执行脚本），则按下列清单自行人工核对：
  - 数字结尾的标签（含带空格的 `year 2024`、`artist:as109`）紧贴 `::` 时必须加逗号空格：`year 2024, ::`；
  - 所有 `数值::` 权重是否都有配对的 `::` 闭合（除非刻意作用到末尾）；
  - 不混用 `(tag:1.2)` 或 `\(` `\)` 等 ComfyUI 转义写法；
  - `Text:` 放在最末尾且未与 `no text` 冲突；
  - 多人角色框内未误写 `1girl`/`2girls`，负面未写 `extra characters`。

### 7. 交付格式（仅输出提示词时）

按下面顺序给，每段放代码块方便复制：

1. 一两句话说明这张图的设计思路，或与上一版改了哪里。
2. 推荐设置：模型、尺寸、步数、Quality Tags 开/关、负面预设选择。
3. **Prompt**（多人或启用角色框时再分 **角色 1**、**角色 2**）
4. **Undesired Content**
5. 需要变体时，只说明“把哪一句换成什么”，不要整段重贴。
6. **看图判断要点**：列 3~4 条验收点与常见跑偏后的微调方向。

**连续修改**（用户看完图说“再改改”）：先判断是 A 微调（只改指定那一处，其余词一个不动）、B 换人保场景，还是 C 全新重写，规则见 `references/rules.md` 第十一节第 4 条。判断不出来按 A 处理并说明一句。

## 权重速查

| 写法 | 效果 |
| --- | --- |
| `1.3::词::` | 加强到 1.3 倍，`::` 结束；不写结尾会一直作用到末尾 |
| `0.6::词::` | 减弱 |
| `-1::词::` | 定向去掉 / 反转（V4.5+） |
| `{词}` / `[词]` | ×1.05 / ÷1.05，每层叠一次 |
| `0.7::artist:as109, ::` | 数字结尾的名字**必须加逗号** |

调一个词先试 1.3~1.5；默认不加权。圆括号在 NAI 里不是权重，不需要 `\(` `\)`。
