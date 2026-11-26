# NovelAI 提示词规则整理（V4.5 / V5 · NSFW 插件适配版）

整理自官方文档、开源社区对照实验与实际跑图结论。每条规则后面标了出处等级：

- 【官方】NovelAI 官方文档原文
- 【实测·他人】开源项目对照实验（锁种子、只改一处）
- 【经验】社区说法，没有对照实验
- 【我们】自己跑图得到的结论

---

## 一、最重要的九条

1. **权重写法是 `1.2::词::`，不是 `(词:1.2)`。** 圆括号在 NAI 里只是普通字符。【官方】
2. **画师名或标签以数字结尾时，写成 `0.7::artist:as109, ::` 或 `1.1::year 2024, ::`，逗号必须有。** 数字紧贴 `::` 会被读成权重值（`na_tarapisu153::` 被当成权重 153），整张图会糊成噪点。【实测·他人】
3. **人数标签（`2girls`、`1girl, 1boy`）放主提示词最前面，角色框里只写 `girl` 或外貌标签不带数字。**【官方】
4. **现成角色（模型认识的）在角色框里只写名字，不要再列发色瞳色。** 多写外貌会和名字打架，权重翻倍。原创角色才要把外貌写全。【实测·他人】
5. **姿势、体位、机位、景别用标签；人和人之间的关系用句子。** 长从句描述肢体，模型会把每个从句各画一遍，多出一条腿。【实测·他人】
   补充：双人动作（磨豆腐）光写标签画不对，加一句写清“谁的腿在哪”才对，见第五节。【我们】
6. **露骨概念光写标签不准，要“标签 + 一句具体描述”**（脱肛、扩张、排蛋、道具都是这样）。【我们】
7. **质量标签和负面预设是客户端/插件拼上去的，手写一遍就叠了两层。** 二选一：开了 Add Quality Tags 就别手写 `masterpiece` 之类。【官方 + 实测·他人】
8. **Add Quality Tags 会带一个 `no text`；Heavy/Light 负面预设带 `multiple views`（Heavy 还有 `screentone`）。** 出分镜、要文字时这两样都会添乱。【官方】
9. **露骨图用 Full，不用 Curated。** V4.5 Curated 的质量标签里直接带 `rating:general` 和 `-0.8::feet::`；V5 Curated 画不出乳头和下身。【官方 + 我们】

---

## 二、主提示词结构

### 顺序

官方给的“期望顺序”【官方】：

```text
人数（1girl / 2girls / 1boy）→ 角色名 → 作品名 → 其他随意
```

社区对 1844 条真实提示词做的统计（各类词谁在前的比例）【实测·他人】：

```text
人数 → 外貌 → 服装 → 表情 → 场景·背景 → 镜头 → 动作 → 质量尾
```

最硬的两条：人数放最前，质量词放最后。V4 以后顺序影响比 V3 小，但重要的东西放前半段仍然有用。【官方】

### 标签和句子怎么分工

V4.5 起标签和自然语言可以混写，V5 对句子理解更好，官方支持英语和日语句子，英语更准。【官方】

| 用标签 | 用句子 |
| --- | --- |
| 姿势：`sitting` `standing` `lying` `kneeling` `squatting` `on back` `on stomach` `on side` `knees up` `legs up` `leg up` `arm support` | 两个人之间谁对谁做什么 |
| 机位：`from above` `from below` `from side` `from behind` | 东西之间的位置关系、归属 |
| 景别：`close-up` `upper body` `cowboy shot` `full body` | 画面里谁占多大 |
| 视线：`looking at viewer` `looking back` | 光影叙事、进行中的动作状态 |
| 关键物件单独拎出来：`transparent umbrella, wooden bench` | 衣服的不对称状态（只穿一只袜子之类） |

【实测·他人】：姿势写成句子 3/3 画糊，写成标签 3/3 成功；`from below` 写成句子 3/3 没压低机位。

### 互斥组（每组只挑一个）【实测·他人】

| 类别 | 只选一个 |
| --- | --- |
| 视线 | looking at viewer / looking to the side / looking up / looking down / looking away / looking back |
| 景别 | close-up / portrait / upper body / cowboy shot / full body / wide shot |
| 背景 | simple background / white background / detailed background / transparent background / blurry background |
| 水平机位 | from side / from behind / straight on |
| 垂直机位 | from below / from above（可以和水平机位组合） |
| 版式 | comic / 4koma / multiple views / reference sheet / sticker |

---

## 三、权重语法【官方】

| 写法 | 效果 | 可用版本 |
| --- | --- | --- |
| `{词}` | ×1.05，每多一层再乘一次（`{{词}}` ×1.1025） | 全部 |
| `[词]` | ÷1.05，每多一层再除一次 | 全部 |
| `1.5::词::` | ×1.5，到 `::` 结束 | V4+ |
| `1.5::词A, 词B::` | 一组一起 ×1.5 | V4+ |
| `0.5::词::` | 减弱到一半 | V4+ |
| `-1::hat::` | 定向去掉（比写进负面更准） | V4.5+ |
| `-1::monochrome::` | 概念反转（这个例子会加上颜色） | V4.5+ |

要点：

- **不写结尾的 `::`，权重会一直作用到提示词末尾。** 一个漏掉的 `::` 会让后面全部被加权。
- 单独一个 `::` 会把前面没配对的 `{` `[` 全部关掉。
- **负面框里的 `{}` 是“更要避开”**，方向跟正面相反。
- 常用数值【实测·他人】：增强常用 1.1 / 1.2 / 1.3 / 1.5 / 2；**1.3 ~ 1.8 是调一个词最值得试的区间**。负权重常用 -1 到 -2（过大易成噪点）。**默认不加权**，某个元素确实出不来再给它加。
- 从 ComfyUI 搬提示词过来要改写法：`(tag:1.2)` → `1.2::tag::`；`\(` `\)` 不需要，直接写圆括号。

---

## 四、画师串

### 写法

- 前缀 `artist:名字` 和直接写名字（`ningen mame`）都有人用；同一个画师两种写法效果不一定一样，第一次用先单画师小图对照。【经验】
- 名字里的空格和下划线：不要假设等价，照原样用。【实测·他人】
- 多画师用**权重梯度**分主次：主导画师 1.0~1.5，辅助的 0.5~0.9 递减。等权平均混合容易互相污染。【经验】
- **数字结尾的画师名一律写 `N::artist:xxx123, ::`**（见第一节第 2 条）。
- 多画师串时 CFG/scale 可以从 3.5~4 起试；单画师从 5~6 起。【经验】
- 画师串和透明背景一起用会出噪点，透明背景只用 ≤2 个画师或者直接写画风词。【实测·他人】
- **使用本插件生图时**：若插件已开启「画风槽位」或默认风格，插件会自动在提示词最前面拼入画师串；调用 `NAI_Generate_Image` 时不要再手动塞第二套画师串。

### 别人验收过的 V5 Full 串【实测·他人】

```text
社区A（日系赛璐璐、线条清晰）
1.1::artist:ningen_mame::, 0.7::artist:karameru::, 0.6::artist:mery::

清新日常（明亮干净）
0.9::artist:shiromanta::, 0.7::artist:karameru::, 0.6::artist:mignon::

厚涂质感（暖调、油画感）
1.3::chen bin::, 0.8::rella::, 0.6::artist:mx2j::

厚涂1（细节密度最高，8 个画师，吃 token）
[artist:cogecha],{{artist:ciloranko}},artist:rella,[ask (askzy)],mignon,artist:kawacy,artist:minaba hideo,[artist:pigeon666]
```

> 避坑：`1.2::artist:ask::, 0.7::artist:mignon::` 在后续同种子 POV 实测中（见 `recipes.md`）会导致皮肤塑料感与人物躯干拉长，不建议直接照搬。

---

## 五、多角色【官方】

- 上限：V4/V4.5 最多 6 个角色，**V5 最多 22 个**。本插件开启多角色块后提供 `char1_prompt` / `char2_prompt`（左 0.25 / 右 0.75）。
- **主提示词放：人数、场景、画风、动作的总体描述。角色框放：这个角色的外貌。**
- 位置：API 里是 `center_x / center_y`（0~1），只要有一个角色给了坐标就要把 `use_coords` 设成 true。
- 互动前缀写在角色框里：`source#动作`（主动方）、`target#动作`（被动方）、`mutual#动作`（互相）。
- **每个角色框可以有自己的负面**，但不要塞一长串别人的特征（会误伤异瞳等自身特点），只防真会串过去的 1~2 个小特征（`braid, cheek mark`）。
- 不用角色框时，可以在一个框里用 `|` 分隔：`2girls, classroom | girl, black hair | girl, blonde hair`。**两种写法不能混用。**
- **多人图负面里不要放 `extra characters`**，它会和人数标签打架，多出人或者少人。【实测·他人】
- 交互句里用外观指代人，每个接触点写清是谁的手碰到谁的哪里。【实测·他人】

我们的结果【我们】：蒂法 + 爱丽丝磨豆腐，`tribadism, scissoring` 标签画不出来；在主提示词里加一整句“谁的腿压在谁的大腿上、下身怎么贴”之后姿势对了，两人长相没串。抬起的膝盖会挡住贴合处，要写腿放低、俯视。

---

## 六、特殊标签【官方】

| 标签 | 作用 | 版本 |
| --- | --- | --- |
| `best quality` → `worst quality`（6 档） | 画质 | 全部 |
| `very aesthetic` `aesthetic` `displeasing` `very displeasing` | 好看程度 | 全部 |
| `masterpiece` | 画质 | V4.5+ |
| `year 2014` 这类 | 画风往那一年的流行风格靠 | V2+ |
| `rating:general` / `rating:sensitive` / `rating:questionable` / `rating:explicit` | 分级 | V4.5+ |
| `low / medium / high / ultra complexity` | 画面复杂度（`high complexity` 易固化构图，默认不加） | V5 |
| `transparent background` `has alpha` `alpha transparency` | 透明背景 | V5 |
| `fur dataset` | 偏兽人画风（放提示词最前） | V4+ |
| `background dataset` | 风景、静物、动物肖像（放最前） | V4.5+ |
| `location` `depthness` `visual novel art` `attractive male` | 其他 V5 新增 | V5 |

---

## 七、质量标签和负面预设原文【官方】

### Add Quality Tags 加在末尾的内容

| 模型 | 追加 |
| --- | --- |
| V5 Full / Curated（Standard） | `, very aesthetic, masterpiece, no text` |
| V5 Full / Curated（Light） | `, very aesthetic, amazing quality, no text` |
| V4.5 Full | `, location, very aesthetic, masterpiece, no text` |
| V4.5 Curated | `, location, masterpiece, no text, -0.8::feet::, rating:general` |

### 负面预设

**V5 Heavy**（V4.5 Full 的 Heavy 同样内容）：
```text
lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page
```

**V5 Light**：
```text
lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::
```

**V5 Human Focus**：Heavy 全部 + `@_@, mismatched pupils, glowing eyes, bad anatomy`

---

## 八、文字渲染【官方】

- 正面加 `text, english text`（中文写 `chinese text`），然后在**主提示词最末尾**写 `Text: 要显示的字`。`Text:` 后面再写别的词会被一起画进图里。
- 多段文字之间空一行（网页里 Shift+Enter）。
- 字数上限（含空格换行）：V4/V4.5 **118**，V5 Curated **374**，V5 Full **750**。
- 语言：V5 支持英文、日文、中文等；V4/V4.5 只有英文。**英日最好，中文偏弱。**
- 文字出不来先关掉 Add Quality Tags（`no text` 在压它）。
- 普通叙述句不要整段加英文双引号；气泡台词要配一个载体（气泡、纸条、招牌）并写清位置。【实测·他人】

---

## 九、参数与插件说明

| 参数 | 建议 | 出处 |
| --- | --- | --- |
| 步数（steps） | 推荐 23~28；插件支持 1~50 步 | 【我们】 |
| Guidance（scale） | 常规 5~6；结构复杂的需求留在 8（同种子测试 8 命中率明显高于 5） | 【官方 + 我们】 |
| 采样器 | 推荐 Euler Ancestral（`k_euler_ancestral`）或 DPM++ 2M（`k_dpmpp_2m_sde`） | 【官方】 |
| Noise schedule | 推荐 `karras`（**V5 仅支持 Karras**） | 【官方 + 插件】 |
| CFG rescale | 常见 0~0.1（部分直连画风用 0.5~1.0），插件上游范围 0~1 | 【实测·他人 + 插件】 |
| Variety+ | API 字段 `skip_cfg_above_sigma`（UI 开关对应 58），构图更多样 | 【经验】 |
| SMEA / DYN | 旧采样参数；**不要往 V5 发** | 【官方 + 实测·他人】 |
| 尺寸 | 第三方 `/ai/` 协议传 `宽x高`（64 倍数，如 `832x1216`、`1216x832`、`1024x1024`、`1024x1536`）；直连传 `竖图`/`横图`/`2K竖图` 等 | 【插件】 |
| 参考图 | **精准参考（director）V4.5 与 V5 均支持**；**Vibe 参考仅限 V4.5**；**img2img 均支持**。单次最多 8 张，精准参考与 Vibe 不可混用 | 【官方 + 插件】 |
| 提示词长度 | V5 Full 约 1471 token、Curated 约 703（软上限，超了也能出图） | 【实测·他人】 |

**实测结论：参数不是让它更听话的杠杆**：构图在前几步就定了，加步数只补细节。要它更听话，改提示词写法（标签 + 具体句 + 难点加权精简装饰），别瞎拉高步数或引导。【我们】

---

## 十、API 核心字段参考（第三方 `/ai/` 协议）

- `v4_prompt.caption.base_caption`：主提示词
- `v4_prompt.caption.char_captions[]`：角色框，每项 `{char_caption, centers:[{x,y}]}`
- `v4_negative_prompt.caption.char_captions[]`：每个角色自己的负面
- `v4_prompt.use_coords`：是否按坐标摆角色
- `director_reference_*`：精准参考数组（V4.5 / V5 均支持）
- `reference_*_multiple`：Vibe 参考数组（仅限 V4.5）
- 注意中转站并发限制：同账号/同源并发可能返回 429 `Concurrent generation is locked`，本插件付费 POST 请求默认不自动重试。

---

## 十一、从中文需求写提示词：四条补充

### 1. 中文里的强调词 → 权重

| 用户怎么说 | 写法 |
| --- | --- |
| 突出、强调、重点、要明显 | `1.2::词::` |
| 非常、极其、最、一定要、特别 | `1.5::词::` |
| 弱化、淡化、稍微、一点点、隐约 | `0.8::词::` |
| 不要、去掉、别有 | **不是减权重**：正文写 `-1::词::` 或放进负面 |

- 一句话里强调的是一整组东西，用组权重 `1.2::词A, 词B::`。加完权重要检查 `::` 是否闭合。

### 2. 现成角色名格式

- 写法照 Danbooru 标签：**下划线换成空格，作品名括号照原样**。例：`kisaki (blue archive)`、`hatsune miku`（不带括号）、`aemeath (wuthering waves)`。
- 括号只在标签本身就带括号时才加，不要自己给不带括号的角色补作品名（`hatsune miku (vocaloid)` 是错的）。
- 开启本插件「内置法典词库（`enable_grimoire`）」时，也可直接通过 `NAI_Search_Grimoire` 工具或 `【法典:词条名】` 展开现成实测组合。

### 3. 生僻概念写法

常见标签（`sitting`、`smile`、`beach`）直接用；生僻概念和露骨/复杂交互概念一律采用“**核心标签 + 一句具体描述**”。

### 4. 连续修改：三档继承

在聊天里用户说“再来一张”“换个姿势”“换成别人”时，先判断属于哪一档，再改上一版提示词：

| 档 | 用户的话像什么 | 怎么改 |
| --- | --- | --- |
| A 微调 | “把背景换成晚上”“加个帽子”“表情害羞一点”“胸小一点” | 上一版整份保留，**只改他说的那一处**，没提的标签一个都不动 |
| B 换人保场景 | “换成另一个角色”“换成某某” | 场景、构图、光照、机位、氛围保留；角色名、外貌、服装、身材换掉 |
| C 全新 | 跟上一版没关系的新要求 | 上一版全部丢掉，从头写 |

- 判断不出来时按 A 处理，并在回复里说一句“我按微调改的，想全换就说”。
- 只改了几处时，回复里列出改了哪几处。
- 用户看图后说“不满意”但没说哪里不满意：先问一句是构图、人物、画风还是细节，别整份重写。

---

## 十二、横图别让耳朵顶出上沿【我们】

1216×832 横图画人，头常常顶到上沿，兽耳被切平。要留出头顶，**写清耳朵上面那一条画什么具体东西**：

- 卧室：`Her head is in the middle of the frame, not near the top: above the tips of her fox ears there is a wide strip of the bedroom wall with the headboard and a string of fairy lights.`
- 窗边站着全身：`She is seen from across the room and takes up about two thirds of the frame height: above the tips of her fox ears there is a wide strip of the bright window and the curtain rail.`

同请求体对照：具体那句 8/9；只说 `a wide strip of the room behind her` 5/8；只靠负面 `head out of frame, ears out of frame, cropped head` 3/9；强行压低角色框坐标会让人物缩太小或躺姿变形，不建议用。
侧躺时横着伸出去的那只耳朵，这一句管不了（会碰到左右两边）。
