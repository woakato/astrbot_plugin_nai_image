# NovelAI 提示词规则整理（V4.5 / V5 · 全年龄插件适配版）

来源于 NovelAI 官方文档、开源社区对照实验与实际出图验证。每条规则按可信度标出层级：

- 【官方】NovelAI 官方文档原文
- 【实测·他人】开源社区锁种子单变量对照实验
- 【经验】社区常用经验
- 【实测】实际跑图验证结论

---

## 一、最重要的九条

1. **权重写法是 `1.2::词::`，不是 `(词:1.2)`。** 圆括号在 NAI 里只是普通字符，不需要 `\(` 转义。【官方】
2. **名称以数字结尾时，写成 `0.7::artist:as109, ::` 或 `1.1::year 2024, ::`，逗号必须有。** 数字紧贴 `::` 会被解析成权重数值（如权重 109 / 2024），整张图会崩成噪点。【实测·他人】
3. **人数标签（`2girls`、`1girl, 1boy`）放主提示词最前面，角色框里只写 `girl` 或外貌标签，不要再带数字。**【官方】
4. **现成角色（模型认识的）在角色框里只写名字，不要再列发色瞳色。** 多写外貌会和名字权重叠加或冲突。原创角色才把外貌写全。【实测·他人】
5. **姿势、机位、景别用标签；人和人之间的互动关系用英文句子。** 长从句描写肢体细节容易让模型多画手脚。【实测·他人】
6. **质量标签和负面预设是客户端/插件拼接的，手写一遍就会叠两层。** 二选一：开启了 Quality Tags 就别再手写 `masterpiece, very aesthetic`。【官方 + 实测·他人】
7. **Quality Tags 自带 `no text`；Heavy/Light 负面预设带 `multiple views`（Heavy 还有 `screentone`）。** 出分镜、要画面文字时这两样会添乱。【官方】
8. **全年龄干净画面用 V5 Full 或 Curated 均可；需要更高自由度写实/细节时优先 V5 Full。** V4.5 Curated 的质量预设自带 `rating:general` 与 `-0.8::feet::`。【官方】
9. **步数建议 24~28，构图在前几步已定型，画不对优先改提示词写法而非盲目拉高步数或引导。**【实测】

---

## 二、主提示词结构

### 顺序

官方推荐顺序【官方】：

```text
人数（1girl / 2girls / 1boy）→ 角色名 → 作品名 → 其他随意
```

社区对 1844 条真实提示词统计的常见顺序【实测·他人】：

```text
人数 → 外貌 → 服装 → 表情 → 场景·背景 → 镜头 → 动作 → 质量尾
```

最核心两条：人数放最前，质量词放最后。V4 以后顺序敏感度比 V3 低，但主体与难点放在前半段依然更稳。【官方】

### 标签和句子怎么分工

V4.5 起标签和自然语言可混写，V5 对英语与日语句子理解更好，其中英语最稳。【官方】

| 用标签 | 用句子 |
| --- | --- |
| 姿势：`sitting` `standing` `lying` `kneeling` `squatting` `arm support` | 两个人之间谁对谁做什么 |
| 机位：`from above` `from below` `from side` `from behind` | 物体之间的位置关系、归属 |
| 景别：`close-up` `upper body` `cowboy shot` `full body` | 画面中主体占比与留白区域 |
| 视线：`looking at viewer` `looking back` | 光影叙事、进行中的动态效果（如水花飞溅） |
| 关键物件：`transparent umbrella, wooden bench` | 服装的不对称状态与质感细节 |

【实测·他人】：姿势和低角度机位（如 `from below`）写成句子容易失效，写成标签更稳定。

### 互斥组（每组只挑一个）【实测·他人】

| 类别 | 只选一个 |
| --- | --- |
| 视线 | looking at viewer / looking to the side / looking up / looking down / looking away / looking back |
| 景别 | close-up / portrait / upper body / cowboy shot / full body / wide shot |
| 背景 | simple background / white background / detailed background / transparent background / blurry background |
| 水平机位 | from side / from behind / straight on |
| 垂直机位 | from below / from above（可与水平机位组合） |
| 版式 | comic / 4koma / multiple views / reference sheet / sticker |

---

## 三、权重语法【官方】

| 写法 | 效果 | 可用版本 |
| --- | --- | --- |
| `{词}` | ×1.05，每多一层叠乘（`{{词}}` ×1.1025） | 全部 |
| `[词]` | ÷1.05，每多一层叠除 | 全部 |
| `1.5::词::` | ×1.5，到 `::` 结束 | V4+ |
| `1.5::词A, 词B::` | 一组一起 ×1.5 | V4+ |
| `0.5::词::` | 减弱到一半 | V4+ |
| `-1::hat::` | 定向去掉（比写进负面更准） | V4.5+ |
| `-1::monochrome::` | 概念反转（此例会转为彩色） | V4.5+ |

要点：

- **不写结尾 `::` 会一直作用到提示词末尾。**
- 单独一个 `::` 会把前面未配对的 `{` `[` 全部关闭。
- **负面框里的 `{}` 表示“更要避开”**，方向与正面相反。
- 常用增强区间为 `1.1`~`1.8`，负权重常用 `-1`~`-2`（过大负权重易导致噪点）；**默认不加权**，确有元素画不出再加。
- 从 ComfyUI / A1111 迁移：`(tag:1.2)` → `1.2::tag::`，去掉 `\(` `\)`。

---

## 四、画师串

### 写法

- 前缀 `artist:名字` 与直接写名字效果可能不同，新画师建议先单画师小图对照。【经验】
- 空格与下划线在分词上未必等价，按社区已验证写法原样使用更稳。【实测·他人】
- 多画师用**权重梯度**分主次：主导画师 `1.0`~`1.5`，辅助 `0.5`~`0.9` 递减，避免等权互抢。【经验】
- **数字结尾画师名必须加逗号**：`0.7::artist:xxx123, ::`（见第一节第 2 条）。
- 多画师串时 CFG/scale 可从 `3.5`~`5` 起试；透明背景建议 `≤2` 个画师或改用画风词，避免噪点。【实测·他人】
- **使用本插件生图时的注意**：插件开启了「画风槽位」或默认风格时，会自动在提示词前拼入对应画师串。调用 `NAI_Generate_Image` 工具时不要再在 `prompt` 里硬写第二套画师串；只有在写给用户手动复制到网页/面板测试时才附带画师串。

### 已验证的全年龄 V5 Full 画师串【实测·他人】

```text
日系赛璐璐（线条清晰）
1.1::artist:ningen_mame::, 0.7::artist:karameru::, 0.6::artist:mery::

清新日常（明亮干净）
0.9::artist:shiromanta::, 0.7::artist:karameru::, 0.6::artist:mignon::

厚涂质感（暖调、油画感）
1.3::chen bin::, 0.8::rella::, 0.6::artist:mx2j::

厚涂精细（细节密度高，8 画师，较占 token）
[artist:cogecha],{{artist:ciloranko}},artist:rella,[ask (askzy)],mignon,artist:kawacy,artist:minaba hideo,[artist:pigeon666]
```

> 注：如果某条画师串在特定构图下出现比例异常或质感变塑料，可尝试先去掉画师串用纯风格词做同种子对照排查。

---

## 五、多角色【官方】

- 上限：V4/V4.5 最多 6 个角色，**V5 最多 22 个**。本插件 LLM 工具开启多角色块后提供 `char1_prompt` 与 `char2_prompt`（对应左 0.25 / 右 0.75）。
- **主提示词放：人数、场景、画风、整体互动动作。角色框放：该角色自身的外貌、表情、服饰。**
- 位置坐标：`center_x / center_y`（`0`~`1`），指定坐标时需将 `use_coords` 设为 `true`。
- 互动前缀写在角色框里：`source#动作`（主动方）、`target#动作`（被动方）、`mutual#动作`（互相）。
- **角色框负面要克制**：不要把另一人的全套外貌词填进去，否则同色系或复杂特征（如异瞳）容易被误伤；仅屏蔽真实会发生串色的单个小特征（如 `braid, cheek mark`）。
- 不用角色框时可在单框内用 `|` 分隔，但两种写法不能混用。
- **多人图负面里不要放 `extra characters`**，否则会与人数标签冲突。【实测·他人】
- 交互句用外貌或穿着指代人物（如 `"the girl in the beige coat hands the cup to the girl with headphones"`）。【实测·他人】

---

## 六、特殊标签【官方】

| 标签 | 作用 | 版本 |
| --- | --- | --- |
| `best quality` → `worst quality`（6 档） | 画质 | 全部 |
| `very aesthetic` `aesthetic` `displeasing` `very displeasing` | 审美档位 | 全部 |
| `masterpiece` | 高画质 | V4.5+ |
| `year 2014` 等年份标签 | 靠拢特定年代流行画风（加权时注意末尾数字加逗号） | V2+ |
| `rating:general` | 全年龄分级约束 | V4.5+ |
| `low / medium / high / ultra complexity` | 画面复杂度（`high complexity` 易固化构图，默认不加） | V5 |
| `transparent background` `has alpha` `alpha transparency` | 透明背景 | V5 |
| `fur dataset` | 偏兽人画风（放最前） | V4+ |
| `background dataset` | 风景、静物、动物肖像（放最前） | V4.5+ |
| `location` `depthness` `visual novel art` | V5 常用辅助标签 | V5 |

---

## 七、质量标签和负面预设原文【官方】

### Add Quality Tags 追加内容

| 模型 | 追加 |
| --- | --- |
| V5 Full / Curated（Standard） | `, very aesthetic, masterpiece, no text` |
| V5 Full / Curated（Light） | `, very aesthetic, amazing quality, no text` |
| V4.5 Full | `, location, very aesthetic, masterpiece, no text` |
| V4.5 Curated | `, location, masterpiece, no text, -0.8::feet::, rating:general` |

### 负面预设参考

**Heavy**（V5 / V4.5 Full）：
```text
lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page
```

**Light**：
```text
lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::
```

**Human Focus**：Heavy 全部 + `@_@, mismatched pupils, glowing eyes, bad anatomy`

---

## 八、文字渲染【官方】

- 正面加 `text, english text`（中文写 `chinese text`），并在**主提示词最末尾**写 `Text: 要显示的字`。`Text:` 后不能再跟其他标签。
- 多段文字之间空一行；单次文字长度参考：V4/V4.5 ≤ 118 字符，V5 Curated ≤ 374，V5 Full ≤ 750。
- 语言支持：V5 支持英/日/中等（英日最稳）；V4/V4.5 仅英文稳定。
- 需要出文字时先关掉 `Add Quality Tags`（因其自带 `no text`）。
- 网页版中普通句子不要随意加英文双引号，否则会被误识别为待渲染文字；气泡台词要指明载体（招牌、气泡、书页）和位置。【实测·他人】

---

## 九、参数与插件协议对照

| 参数 | 建议与插件说明 | 出处 |
| --- | --- | --- |
| 步数（steps） | 推荐 24~28；本插件支持 1~50 步 | 【实测】 |
| 引导（scale） | 默认 5~6 画面柔和；结构复杂需求可用 8；本插件第三方 `/ai/` 限 0~10 | 【官方 + 实测】 |
| CFG Rescale | 常用 `0`~`0.1`（部分画风/直连预设用 `0.5`~`1.0`），本插件上游有效范围 `0`~`1` | 【官方 + 插件】 |
| 采样器 | 推荐 `k_euler_ancestral` 或 `k_dpmpp_2m_sde` / `k_dpmpp_2m` | 【官方】 |
| Noise schedule | 推荐 `karras`（**V5 仅支持 Karras**，选其他会被插件拦截） | 【官方 + 插件】 |
| SMEA / DYN | 旧采样参数，**不要往 V5 发** | 【官方】 |
| 尺寸 | 第三方 `/ai/` 协议传 `宽x高`（64 倍数，常用 `832x1216`、`1216x832`、`1024x1024`、`1024x1536`）；直连传 `竖图`/`横图`/`方图`/`2K竖图` 等 | 【插件】 |
| 参考图 | **精准参考（director）V4.5 与 V5 均支持**；**Vibe 参考仅限 V4.5**；**img2img 均支持**。单次最多 8 张，精准参考与 Vibe 不可混用 | 【官方 + 插件】 |
| 提示词长度 | V5 Full 约 1471 token、Curated 约 703（软上限） | 【实测·他人】 |

**参数调优铁律**：先锁定提示词结构和标签，不要试图靠拉高步数或引导去“修”逻辑错误。结构靠标签和主句定型，参数只影响锐度与质感。

---

## 十、API 核心字段参考（第三方 `/ai/` 协议）

- `v4_prompt.caption.base_caption`：主提示词
- `v4_prompt.caption.char_captions[]`：角色框数组，每项 `{char_caption, centers:[{x,y}]}`
- `v4_negative_prompt.caption.char_captions[]`：各角色独立负面
- `v4_prompt.use_coords`：是否启用坐标定位
- `director_reference_*`：精准参考（`images` / `descriptions` / `strength_values` / `secondary_strength_values` / `information_extracted`）
- `reference_*_multiple`：Vibe 参考（仅限 V4.5）

---

## 十一、从中文需求写提示词与连续修改

### 1. 中文强调词 → 权重

| 用户说法 | 写法 |
| --- | --- |
| 突出、强调、重点、明显 | `1.2::词::` |
| 非常、极其、最、一定要、特别 | `1.5::词::` |
| 弱化、淡化、稍微、一点点、隐约 | `0.8::词::` |
| 不要、去掉、别有 | 正文写 `-1::词::` 或放入负面（不要映射成 0.8） |

强调一整组概念可用组权重 `1.2::词A, 词B::`，加完注意检查 `::` 是否闭合。

### 2. 现成角色名格式

- 遵循 Danbooru 标签习惯：下划线改空格，只在标签本身带括号时保留作品名括号，如 `kisaki (blue archive)`、`hatsune miku`（不要强行补 `(vocaloid)`）。
- 本插件开启「内置法典词库（`enable_grimoire`）」时，也可直接用 `NAI_Search_Grimoire` 工具检索或使用 `【法典:词条名】` 展开现成实测组合。

### 3. 生僻概念写法

常见标签（`sitting`、`smile`、`beach`）直接用；生僻概念或复合互动动作若单独标签画不准，采用 **“核心标签 + 一句具体英文外观/动作描述”**。

### 4. 连续修改：三档继承

| 档位 | 用户意图示例 | 修改策略 |
| --- | --- | --- |
| A 微调 | “背景换成夜晚”“加个贝雷帽”“笑容收敛一点” | 保留上一版全部结构，**只修改指定那一处**，其余词不动 |
| B 换人保场景 | “换成另一个角色”“衣服和场景不变” | 保留场景、构图、光照、机位、氛围；仅替换角色名、外貌与专属服装 |
| C 全新 | 与上一版无关的新需求 | 丢弃上一版，重新从头设计 |

判断不清时按 A 处理，并在回复中注明；只改了几处时直接说明改了哪几处。用户只说“不满意”但未说明原因时，先询问是构图、人物、画风还是细节问题，不要整段重写。
