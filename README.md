# astrbot-plugin-nai-image

基于(NovelAI) 的 AstrBot 生图插件。
### 调用模式

> 插件设置第一项 **「调用模式」** 决定聊天调用（`/image` 指令、陪伴联动）走哪套后端，二选一：
> - **传统 GET（nai.sta1n.cn）**：显示「生图 Token」「生图服务地址」等字段，文生图走 `GET /generate`；
> - **OpenAI 兼容（/v1/images）**：显示「OpenAI 兼容生图接口地址」「密钥」「模型名」等字段，文生图走 `POST /v1/images/generations`。有参考图时按「参考图模式」（`openai_reference_mode`）三选一：**Vibe 参考**（风格/角色参考，`reference_image_multiple`）、**img2img**（`POST /v1/images/edits` 图生图重绘）、**精准参考 director**（`director_reference_*`，支持 nai-diffusion-4-5 与 nai-diffusion-5 系列模型；描述词 `openai_director_caption` 可选 `character&style` / `character` / `style`）。
>
> 面板里的「调用格式」是独立测试入口，可临时切换两种格式；OpenAI 兼容格式支持本地上传参考图。陪伴联动传参考图时按全局调用模式路由。

> 🆕 **v2.3.0 直连联动**：陪伴插件「我会永远陪着你」的生图后端可直接选择 **「只用 NAI 生图插件（直连）」**，进程内调用本插件扩展接口、无需本地代理，支持英文自然语言直接生图（不转译）。详见下方「联动插件」一节；旧的 8765 本地代理方式仍可用。v2.3.4 起直连模式已支持接收陪伴插件传来的参考图并路由到 OpenAI 兼容格式图生图。

> 🆕 **v2.4.0 OpenAI 兼容接口全面对齐**：`/v1/images/generations` 与 `/v1/images/edits` 按接口文档对齐——高级参数（`steps`/`scale`/`sampler`/`noise_schedule`/`seed`/`negative_prompt`）统一放入 `parameters` 对象；参考图支持 **Vibe 参考**（`reference_image_multiple` 数组）、**img2img 图生图** 与 **精准参考 director**（`director_reference_*`，§6）三种模式（配置项「参考图模式」+「精准参考描述」）；新增 **多角色坐标控制**（`use_coords`/`characterPrompts`/`v4_prompt`，§7），聊天指令用可重复的 `--char="提示词|x|y"`，面板可动态添加角色行；超限参考图自动等比缩小（最长边 1920 / 面积 3686400 内），参考图统一以 Data URI 提交；新增「随机种子」「请求超时」「失败重试次数」配置；仅对 408/429/502/503/504、超时及上游"服务繁忙"类瞬时错误按 2/4/8 秒退避重试。本地代理的 `/v1/images/edits` 同时支持 JSON 与 multipart 请求。测试面板新增参考图模式、精准参考描述、噪声强度、种子、多角色坐标与 **director-tools 工具**（抠图/线稿/草图/上色/情绪/清理）入口。

> 🆕 **v2.5.0 多张参考图**：Vibe 参考与精准参考（director）支持一次提交最多 **8 张**；**修复 NAI 5 系列被误判不支持精准参考的问题**（v4.5f / v5f 实测均可用，不再强制切换模型） 参考图（§5.2，超出自动截断），vibe 的 `reference_image_multiple` / `reference_strength_multiple` 与精准参考的五个 `director_reference_*` 数组均按提交顺序逐张对应：
> - 测试面板：参考图选择框支持一次多选上传，缩略图列表可删除单张，并逐张设置强度；精准参考模式下每张还可单独指定 `base_caption`（character&style / character / style）。img2img 模式仍只使用第一张主输入图（§5.3），全局「重绘强度」「附加噪声」仅在 img2img 下显示。
> - 插件设置：新增「Vibe 参考权重」（`openai_vibe_strength`，默认 0.6）、「精准参考权重」（`openai_director_strength`，默认 1.0）与「精准参考次级特征权重」（`openai_director_secondary_strength`，默认 0.5），作为聊天指令 / LLM 工具 / 陪伴联动及面板新参考图的默认权重；「精准参考兜底参考图」（`openai_director_fallback_images`）从"只取第一张"升级为按顺序使用全部已配置图片（最多 8 张）；「精准参考描述」（`openai_director_caption`）作为默认描述按顺序应用到全部兜底参考图。
> - 陪伴直连：陪伴插件传来的 `reference_image_paths` 数组会被完整接收并按下标路由到 vibe / 精准参考数组，不再只取首项。

> ⚠️ **精准参考使用说明**：精准参考（director）支持 nai-diffusion-4-5 与 nai-diffusion-5 全系列（实测 v4.5f / v5f 均可用；插件在其他模型下会自动切换到 4-5-full）；`director_reference_information_extracted` 必须为 `1.0`，其他取值会被上游参数校验拒绝（HTTP 400），插件已按此固定。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/image <提示词>` | 根据提示词生成图片 |
| `/image <提示词> --n=4` | 生成 4 张图片 (1-6) |
| `/image <提示词> --style=anime` | 指定风格 |
| `/image <提示词> --size=横图` | 指定比例 |
| `/image <提示词> --cfg=0.3 --scale=6 --steps=28` | 单次覆盖生成参数 |
| `/quota` | 向上游查询 token 剩余额度与启用状态 |
| `/imgstatus` | 检查本地代理与 token 配置状态（不再在线探测上游） |
| `/nai_interrogate` / `/反推` | 使用配置的多模态模型分析附带图片并输出 NAI tags；默认只反推，不自动生图 |

### 参考图反推

配置 `interrogate_provider` 选择支持图片输入的多模态 Provider 后，可直接发送带图消息并使用 `/反推`，也可以传入本地路径或 URL：

```text
/反推
/nai_interrogate C:\\images\\reference.png 保留人物外貌，只改变姿势
```

反推结果只作为文本返回，不会自动调用 NAI 生图，也不会写入图片历史或服装缓存。模型必须支持 AstrBot Provider 的 `image_urls` 参数；本地图片会在请求时转换为 data URL。

所有参数都只影响当前指令，不会修改插件配置。含空格的参数值需要使用单引号或双引号：

```text
/image 1girl, solo --style=custom --artist="best quality, artist:foo" --negative="bad anatomy, blurry, text"
```

多角色坐标控制（仅 OpenAI 兼容调用模式，`--char` 可重复，坐标 0-1，x 从左到右、y 从上到下，最多 6 个，建议搭配横图）：

```text
/image 2girls, school uniform --char="1girl, red dress|0.3|0.5" --char="1boy, blue suit|0.7|0.5"
```

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `--n` | `1-6` | 生成数量 |
| `--style` | `vertical` / `comicDoujin` / `r18` / `lolita25d` / `anime` / `galgame` / `custom` | 也接受「自定义」及各风格的中文显示名 |
| `--size` | `portrait` / `landscape` / `square` 及 2K/4K 形式 | 也接受「竖图 / 横图 / 方图」等中文值 |
| `--steps` | `1-100` 整数 | 采样步数 |
| `--scale` | `0-20` 数字 | 提示词引导强度 |
| `--cfg` | `0-30` 数字 | CFG Rescale，支持小数和 `0` |
| `--sampler` | 固定采样器名 | 与配置面板可选值一致 |
| `--noise` | `karras` / `native` / `exponential` / `polyexponential` | `--noise_schedule` 是等价别名 |
| `--translate` | `关闭/off` / `开启/on` / `自动/auto` | 单次覆盖转译模式 |
| `--template` | `关闭/off` / `开启/on` | 单次决定是否拼接角色预设 |
| `--model` | 模型名 | 仅允许字母、数字、点、下划线和连字符 |
| `--artist` | 画师串 | 仅在有效风格为 `custom` / `自定义` 时可用 |
| `--negative` | 反向提示词 | 可使用 `--negative=""` 清空当次反向提示词 |
| `--char` | `提示词\|x\|y`，可重复 | 多角色坐标控制（仅 OpenAI 兼容调用模式）；坐标 0-1，最多 6 个 |

采样器可选：`k_dpmpp_2m_sde` / `k_dpmpp_2m` / `k_dpmpp_sde` / `k_dpmpp_2s_ancestral` / `k_euler_ancestral` / `k_euler` / `ddim`。未知参数、重复参数、超出范围或未闭合引号会直接报错，不会发起生图请求。

`bot_reply_mode` 控制 `/image` 指令的回复内容：

- `仅图片`：成功时只发送图片，失败报错保持不变
- `简洁`：生成前只发送状态和最终生效的核心参数，不发送提示词
- `完整`：发送完整提示词、最终核心参数及本次指定的画师串/反向提示词

## 试用点数+测试面板
插件自带一个 **NAI 生图测试面板**，可在 AstrBot 管理后台的插件扩展页面直接在线调试生图参数  
并公益三次免费的生图机会以供尝试(致谢@啊·羽绒服的分享)，请理性下单购买额度。

**关于nai实用站点分享**
> 画风寻找：[`NAI4.5进阶魔法书`](https://docs.qq.com/doc/DR25Xd1hSa1BXVnpx)，[图书馆](https://lib.luoheyan.xyz/)  
> nai进阶法典目录：[`法典目录`](https://nai-bot.pages.dev/%E6%B3%95%E5%85%B8/%E6%B3%95%E5%85%B8%E7%9B%AE%E5%BD%95/)
> nai角色tag查询 (https://www.downloadmost.com/NoobAI-XL/danbooru-character/) , (https://docs.qq.com/sheet/DRFBYSHNoUkRqZlVv?tab=BB08J2) , (https://docs.qq.com/sheet/DWGxXbEZxdmtXSERT?tab=BB08J2)  
> 如果实在找不到角色tag，可以根据同游戏其他角色tag猜测。比如：明日方舟艾雅法拉tag：eyjafjalla \(arknights\)，缪尔赛思角色名：muelsyse，同理可得：缪尔赛思tag：muelsyse \(arknights\)。

## 工具

`NAI_Generate_Image`：接受提示词、风格和尺寸，调用 NAI 生成一张图片并直接发送给当前用户，同时向 Agent 返回单一工具结果以保留完整对话历史。每个消息事件最多请求一次 NAI；模型重复调用时仍会收到对应工具结果，但不会再次生图或扣费，也不需要搭配 `send_message_to_user`。

## 测试面板

插件自带一个 **NAI 生图测试面板**，可在 AstrBot 管理后台的插件扩展页面直接在线调试生图参数。

### 功能

- **双提示词输入**：分别填写「NAI 风格提示词」（英文标签）和「自然语言提示词」（中文/英文描述），后端自动转译并合并
- **合并步骤展示**：生成完成后展示完整的提示词转译与合并过程
- **参数全可调**：采样器、步数、Scale、CFG、模型、风格、负面词等均可在面板直接修改
- **结果即时预览**：生成的图片直接在网页展示，不发送到聊天
- **状态缓存**：面板输入自动保存到浏览器 `localStorage`，下次打开自动恢复
- **试用生成**：未配置密钥的用户可使用内置公益密钥试用 3 次（感谢 @啊·羽绒服 的免费试用额度的密钥分享）

### 面板独立于主配置

测试面板的生图参数**完全独立**于插件 Settings 中的角色预设和模板设置，不会合并 Settings 里的画师串或角色预设，确保调试环境纯净。

## 配置

插件管理面板填写 `image_gen_key`（必填）及其他高级参数。
详细配置项见 `_conf_schema.json`。

### 生图历史

开启 `save_image_history` 后，所有成功生成的图片都会保存到：

```text
data/plugin_data/astrbot_plugin_nai_image/image_history/
```

开启 `save_generation_parameters` 后，每张图片旁会生成一个同名 `.yaml` 文档，记录实际发送给生图接口的参数，但不会保存 Token。输入提示词中的换行会在生图前统一转为逗号分隔，连续水平空白会压缩为单个空格；其他多行参数值仍使用 YAML 字面量块保留换行。此选项需配合 `save_image_history` 使用。

`image_history_limit` 设置本地最多保留的图片数量。每次保存新图片后，插件会删除超出数量的最旧图片及其同名参数文档；设为 `0` 时只保存、不自动清理。归档或清理失败不会影响图片正常返回。

### 提示词转译中间层

可选使用一个 LLM 中间模型，在把 prompt 拼到预设之前先把自然语言描述翻译成 SD / NAI 标签风格。

- `enable_translate`：`关闭`（默认）/ `开启` / `自动`（旧版布尔值会在插件加载时自动迁移为对应字符串）
- `translate_provider`：通过 WebUI 的 provider 下拉选择器选择，留空则使用 AstrBot 默认 provider

强烈建议选用轻量便宜的小模型，转译耗时通常在 1 秒内。转译失败 / provider 不可用时会自动回退原文，不影响出图主流程。

- **开启**：与旧版开启开关的行为一致，整个提示词都会交给 LLM 转译
- **自动**：语法感知分段后保留 NAI 标签，只把自然语言片段合并后交给 LLM，再将转译结果拼回保留标签；纯 NAI 提示词不会调用 LLM
- **自动模式显式标记**：在无法可靠区分的英文短语前添加 `|nl|`，其后的内容会作为自然语言处理。例如：`1girl, solo, best quality |nl| standing in rain with a clear umbrella`
- 转译结果会自动剥离思考型模型（如 MiniMax-M3、DeepSeek-R1）输出的 `<think>...</think>` 思考块，只保留纯标签正文
- **关闭转译时**：服装缓存池 / 默认服装补全等预处理会一并停用，仅保留「角色核心关键词与身体描述补全」（`character_preset`）的拼接；提示词不做转译，仅统一换行和连续空白
- **自动识别为纯 NAI 时**：同样跳过服装缓存和默认服装补全，避免修改原生标签与权重语法
- **与「我会永远陪着你」联动时**：陪伴插件已新增「NAI联动模式」（生图提示词表达方式），开启后由陪伴插件的 LLM 直接按 NAI 4.5 规范输出原生标签 prompt，此时无需再开启本插件的转译——关闭 `enable_translate` 即可，提示词仅做空白规范化后提交生图；使用直连模式时提示词先按 `companion_prompt_format` 处理，再由本插件的转译模式决定是否转译（v2.3.5 起直连生图同样遵循 `enable_translate`，默认「关闭」保持原样提交），详见下方「联动插件」一节

### 服装缓存池（Outfit Cache）

> 在转译模式为「开启」时生效；「自动」模式下只对识别出的自然语言片段生效。关闭转译或自动识别为纯 NAI 时，本功能不参与处理。

设置面板提供「启用服装缓存池」开关（v2.3.6 起默认开启）：关闭后不再写入或读取缓存，「服装缓存时长」选项也会随之隐藏（时长设为 0 同样停用缓存写入）。

很多角色在"今天穿了什么"在一天内是固定的，但是角色在一天内也会换装，cosplay等等。  
- `本插件新增缓存池，缓存池机制，能记录bot换过什么服装并在缓存时间内替换默认服装并保持。  

**行为流程：**

~~~mermaid
graph TD
    A[用户提示] --> B{是否有具体服装词/换装}
    B -->|是| C[存入缓存，自然语言层追加]
    B -->|否| D[检查缓存]
    D --> E{缓存是否有数据}
    E -->|是| F[自然语言层追加缓存服装]
    E -->|否| G[使用默认服装]
    C --> H[自然语言转SD tags]
    F --> H
    G --> I
    H --> I[模板合并]
    I --> J[生成图片]
~~~

## 联动插件：astrbot_plugin_private_companion（我会永远陪着你）

本插件可以作为 [`astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion)（"我会永远陪着你"）的生图后端。自 v2.3.0 起推荐使用**直连模式**：陪伴插件在进程内直接调用本插件的扩展接口完成生图，不需要本地 OpenAI 兼容代理。

> 直连模式需要陪伴插件包含「生图后端 → 只用 NAI 生图插件（直连）」选项（v6.2.3+；该功能已随[上游 PR #137](https://github.com/menglimi/astrbot_plugin_private_companion/pull/137) 提交）。陪伴插件版本较旧时，请先使用下方的「方式二：本地代理」。

### 方式一：直连模式（v2.3.0+，推荐）

**使用步骤**

1. 安装并启用本插件与陪伴插件；
2. 陪伴面板 → 功能开关 → 长线主动 → 主动/用户生图 → **生图后端** 选择 **「只用 NAI 生图插件（直连）」**；
3. 本插件保持 `enable_companion_link` 开启（默认开启）。开启直连后，设置面板会自动隐藏本地代理相关选项（「启用本地 OpenAI 兼容代理」「本地代理端口」）——直连不走本地代理，这些选项不再需要；如需使用旧式代理，先关闭直连即可看到它们。「提示词转译模式」「转译模型供应商」始终可见（v2.3.5 起不再随直连隐藏），且直连生图同样遵循转译模式：默认「关闭」时行为与原来一致，若陪伴侧已输出原生 NAI 标签或英文自然语言直入，请保持「关闭」避免二次转译。
4. 在 `companion_prompt_format` 里选择提示词模式：
   - **自然语言模式（en）**（默认）：把陪伴插件发来的背景信息与需求合并成英文自然语言，**原样直接提交 NAI、不做 LLM 转译**（新版 NAI 已支持英文自然语言直入）。画面描述请用英文写。
   - **nai tag模式**：按 NovelAI 标签规则归一化后透传，适合陪伴侧直接输出原生 tag 的场景；
5. 完成。主动带图、用户指令生图、规则快判、主链 `pc_generate_photo` 工具都会走这条直连通道。

**行为说明**

- 生成图片保存到 `data/plugin_data/astrbot_plugin_nai_image/companion_images/`，并按 `companion_image_retention_days` 自动清理（0 表示不清理）；
- 直连已支持参考图：陪伴侧传来的 `reference_image_paths` 数组（兼容旧的单一 `reference_image_path`）会被完整接收，vibe / 精准参考模式按下标逐张生效；若插件配置了 OpenAI 兼容格式站点（`openai_api_base_url`），则按「参考图使用模式」路由到对应接口，否则回退 NAI 直连文生图；`size` / `ratio` / `style` 未传或无法识别时使用本插件的默认尺寸与风格，常见的 `1024x1024`、`9:16` 等写法会自动归一化；
- 陪伴面板里仅对"我会画给你看"/本地后端生效的配置（参考图一致性、生图风格、负面提示词等）在选择直连后会自动隐藏，统一在本插件里配置；
- 能力查询只做本地就绪判断（token 与会话），不再在线探测上游，因此响应即时；上游真实失败会在生图结果里明确返回，不会假装出图；
- 直连不依赖本地代理：本地代理由「启用本地 OpenAI 兼容代理」独立控制（默认开启；仅影响下方"方式二"）；
- **绕过梯子直连生图站**：「绕过系统代理直连生图站」默认开启，请求 nai.sta1n.cn 时忽略系统/环境代理强制直连，开梯子也不会把生图请求带偏；若必须走代理才能访问生图站再关闭。TUN 虚拟网卡类梯子需在梯子软件里给 nai.sta1n.cn 加直连（DIRECT）规则。

### 方式二：本地 OpenAI 兼容代理（旧方式，仍可用）

插件启动后会在 `127.0.0.1:8765` 起一个 **OpenAI Images API 兼容** 的本地代理（`enable_proxy` 控制，默认开启；端口可在配置里修改），让陪伴插件把生图请求转发到这里，由 nai.sta1n.cn 完成实际生图。注意：启用「陪伴插件直连」时设置面板会隐藏代理相关选项，先关闭直连才能看到 `enable_proxy` 开关。

在 `astrbot_plugin_private_companion` 的生图设置里：

1. **生图方式** 选择"在线 API 生图"
2. **API 地址** 填写：`http://127.0.0.1:8765/v1`
3. **API Key** 任意填写一个占位符即可（NAI 插件不校验）
4. **模型名** 任意填写一个占位符即可（NAI 插件不校验）

这样配置后，当陪伴插件发出生图请求时，本插件会**自动监听本地代理端口**，接管请求、调用 nai.sta1n.cn 出图，然后把 base64 图片传回陪伴插件。

### 🆕 NAI联动模式（提示词表达方式）

陪伴插件在 **「长线主动 → 主动拍照/生图 → 生图提示词表达方式」** 中提供 **「NAI联动模式（NovelAI 标签语法）」**：

- 开启后由陪伴插件的 LLM **直接生成原生 NAI 标签语法**的 prompt（`{tag}` 加权 / `[tag]` 降权、多角色包裹等 NAI 4.5 规范）；
- 直连模式下请把本插件的 `companion_prompt_format` 设为 **「nai tag模式」**，标签 prompt 会原样提交；设为「自然语言模式（en）」时请让陪伴侧输出英文自然语言；
- 此模式下请**关闭本插件的 `enable_translate`**：避免二次转译破坏原生语法；
- 两种方案二选一：**陪伴插件输出原生标签 + 本插件 tag 模式**，或 **陪伴插件输出英文自然语言 + 本插件自然语言模式**。

> 本插件的 `image_gen_key` 仍然要填写真实的token
> 本人与站点和平台没有任何关系，只是作为分享生图平台并不牟利，生图平台也是完全公益。请不要进行二次转接分发，不要因为你一个人让我们都没得用  

---

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

---

> 如果有其他想法可以提 Issue 或 PR。  
