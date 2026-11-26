# astrbot_plugin_nai_image

AstrBot 的 NovelAI 生图插件。支持 `nai.sta1n.cn` 直连与第三方 NovelAI 官方 `/ai/` 协议接口（如星桥及已切至官方协议的中转站），带聊天指令、LLM 工具、网页测试面板与内置画廊，也能给陪伴插件当生图后端。

当前版本 v2.10.2，改动记录见 [CHANGELOG.md](CHANGELOG.md)。

## 上手

设置页选「生图接入方式」，填好凭据就能用 `/image` 出图。

| 方式 | 必填 |
| --- | --- |
| `direct` | `image_gen_key`，即站点申请到的 toUserId；`base_url` 默认 `https://nai.sta1n.cn` |
| `openai` | 填写支持 NovelAI 官方 `/ai/` 协议的接口地址与 `API Key`；支持同时配置渠道 1 与渠道 2（备选，如星渊 + 星桥），在设置页或测试面板随时切换 |

其他项保持默认即可。设置页按当前方式显示用得上的字段，切换不会丢另一套配置；旧版 `starbridge_api_*` 凭据会在启动时自动合并迁移。全部字段的含义和默认值见 `_conf_schema.json`，面板上也有说明。

## 指令

| 指令 | 用途 |
| --- | --- |
| `/image <提示词>` | 生图 |
| `/price` | 试算点数并查余额，不生图 |
| `/quota` | 查 token 余额和启用状态 |
| `/usage` | 点数用量统计，支持 `/usage 30` / `today` / `all` |
| `/imgstatus` | 看当前后端与配置状态 |
| `/反推` | 图片反推 NAI tags，别名 `/nai_interrogate` |
| `/法典` | 检索内置法典词库，别名 `/grimoire` |

`/image` 的参数只作用于本次调用，不改配置：`--n`(1-6)、`--style`、`--size`、`--steps`(1-50)、`--scale`、`--cfg`(0-1)、`--sampler`、`--noise`、`--translate`、`--template`、`--model`、`--artist`、`--negative`、`--char`。含空格的值要加引号，未知参数、越界值和未闭合引号会直接报错，不会发请求。

```text
/image 1girl, solo --style=custom --artist="best quality, artist:foo" --n=4
/image 2girls, school uniform --char="1girl, red dress|0.3|0.5" --char="1boy, blue suit|0.7|0.5"
```

`--char` 可重复，坐标 0 到 1，最多 6 个角色，仅官方协议接口模式可用。

## 两种后端

直连走 `POST /api/web/jobs` 提交任务、轮询下载；上游没有任务接口（404）时回退 `GET /generate`。`request_timeout` 默认 180 秒，两种模式共用。

第三方官方协议接口（原 OpenAI 兼容格式已移除）统一走 `/ai/` 原生协议（`/ai/generate-image`、`/ai/encode-vibe`、`/ai/augment-image`），支持 V4.5 / V5 的 Full 与 Curated 型号，尺寸写 `宽x高`（64 的倍数）。参考图由 `openai_reference_mode` 决定，一次最多 8 张：

| 模式 | 行为 |
| --- | --- |
| `vibe` | 风格或角色氛围参考，仅限 V4.5，先逐图编码再按顺序与逐图权重生图，默认权重 0.6 |
| `img2img` | 只用第一张作底图重绘，V4.5 / V5 均支持 |
| `director` | 精确提取角色或画风，V4.5 与 V5 均支持（V4.5 主权重 0-1、次级默认 0.5；V5 主权重 0-5，次级固定 0、提取度固定 1） |

Vibe 编码独立计费，付费 POST 请求不自动重试，也不跟随重定向。开启「主渠道不可用时自动切换备选」后，仅在未配置、连接失败或返回 401/403/429/502/503（且 Vibe 未产生编码扣费）时切到另一条渠道；超时与 400 参数错误不会自动切换，避免重复提交扣费请求。

## 点数

直连按「步数 × 画质」计价（点/张）：

| 步数 | V4.5 标准 | V5 标准 | 2K | 4K |
| --- | --- | --- | --- | --- |
| 1-28 | 1 | 8 | 15 | 25 |
| 29-35 | 6 | 12 | 19 | 29 |
| 36-45 | 8 | 14 | 21 | 31 |
| 46-50 | 10 | 16 | 23 | 33 |

`/price` 可试算，成功出图会按日记到 `data/plugin_data/astrbot_plugin_nai_image/usage_stats.json` 供 `/usage` 查询。第三方接口的实际计费由中转站决定，插件按像素画幅估算，仅供参考。

## 测试面板

管理后台的插件扩展页里有测试面板：调参数出图、网页预览、上传多张参考图、看提示词合并过程和用量图表。面板参数独立于 Settings，没配密钥可以用内置公益密钥试用 3 次，感谢 @啊·羽绒服 分享额度。

## 陪伴联动

给 [`我会永远陪着你`](https://github.com/menglimi/astrbot_plugin_private_companion) 当生图后端。直连和本地代理在设置页二选一，开启一项另一项会自动关闭并隐藏。

直连（`enable_companion_link`，默认开）在陪伴面板「生图后端」选「只用 NAI 生图插件（直连）」，进程内调用，不占端口，陪伴插件需 v6.2.3 以上。本地代理（`enable_proxy`，默认关）在 `127.0.0.1:8765` 起 OpenAI Images 兼容服务，陪伴侧 API 地址填 `http://127.0.0.1:8765/v1`，Key 和模型名填占位符即可。

陪伴侧输出原生标签时，把 `companion_prompt_format` 设为「nai tag模式」、`enable_translate` 设为「关闭」，避免二次转译。

## 可选功能

| 设置项 | 默认 | 作用 |
| --- | --- | --- |
| `enable_translate` | 关闭 | 把自然语言交给 LLM 转译成 NAI 标签，「自动」只转自然语言片段。关闭时服装缓存和默认服装补全一并停用，只保留 `character_preset` |
| `enable_artist_slots` | 开 | 画风存成「名称 / 模型 / 画师串」，设置页和面板共用；关闭时回退 `image_style` |
| `enable_grimoire` | 关 | 内置 19,663 条实测 Tag，提示词里写 `【法典:词条名】` 自动展开 |
| `enable_nsfw_skill` | 关 | 插件内置 `nai-prompting` 提示词编写 Skill 默认使用全年龄（SFW）版；开启后切换为包含成人向规范与实测配方的 NSFW 版 |
| `enable_llm_tool` | 关 | 开启后模型可调用 `NAI_Generate_Image`；`enable_character_blocks` 加双人角色块，`enable_tool_style` 控制模型能否指定画风 |
| `save_image_history` | 开 | 测试面板与日常生图（`/image`、LLM 工具、陪伴联动、本地代理）默认将图片与提示词存到 `data/plugin_data/astrbot_plugin_nai_image/image_history/`，可在测试面板画廊浏览、搜索、填入参数或删除；`save_generation_parameters` 默认开启并另存同名 `.yaml` 完整参数 |

## 关于

`image_gen_key` 要填自己的真实 token。我和生图站点、平台没有关系，也不从中牟利，站点本身是公益的。请不要二次转接分发，别因为一个人搞得大家都没得用。

有问题或想法，提 Issue 或 PR。
