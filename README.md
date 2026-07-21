# astrbot-plugin-nai-image

基于 [nai.sta1n.cn](https://nai.sta1n.cn) (NovelAI) 的 AstrBot 生图插件。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/image <提示词>` | 根据提示词生成图片 |
| `/image <提示词> --n=4` | 生成 4 张图片 (1-6) |
| `/image <提示词> --style=anime` | 指定风格 |
| `/image <提示词> --size=横图` | 指定比例 |
| `/quota` | 查询 token 剩余配额 |
| `/imgstatus` | 检查生图服务连通性 |

风格：`vertical` / `comicDoujin` / `r18` / `lolita25d` / `anime` / `galgame` / `custom`
尺寸：`竖图` / `横图` / `方图`

## 试用点数+测试面板
插件自带一个 **NAI 生图测试面板**，可在 AstrBot 管理后台的插件扩展页面直接在线调试生图参数  
并公益三次免费的生图机会以供尝试(致谢@啊·羽绒服的分享)，请理性下单购买额度。

**关于nai实用站点分享**
> 画风寻找：[`NAI4.5进阶魔法书`](https://docs.qq.com/doc/DR25Xd1hSa1BXVnpx)  
> nai进阶法典目录：[`法典目录`](https://nai-bot.pages.dev/%E6%B3%95%E5%85%B8/%E6%B3%95%E5%85%B8%E7%9B%AE%E5%BD%95/)
> nai角色tag查询 (https://www.downloadmost.com/NoobAI-XL/danbooru-character/)(https://docs.qq.com/sheet/DRFBYSHNoUkRqZlVv?tab=BB08J2)(https://docs.qq.com/sheet/DWGxXbEZxdmtXSERT?tab=BB08J2)

## 工具

`NAI_generate_image`：参数与 `/image` 命令相同，调用NAI生成图片并保存到本地，输出文件路径。

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

### 提示词转译中间层

可选开启一个 LLM 中间模型，在把 prompt 拼到预设之前先把自然语言描述翻译成 SD / NAI 标签风格。

- `enable_translate`：`true` 开启，`false` 关闭（默认关闭）
- `translate_provider`：通过 WebUI 的 provider 下拉选择器选择，留空则使用 AstrBot 默认 provider

强烈建议选用轻量便宜的小模型，转译耗时通常在 1 秒内。转译失败 / provider 不可用时会自动回退原文，不影响出图主流程。

### 服装缓存池（Outfit Cache）

很多角色在"今天穿了什么"在一天内是固定的，但是角色在一天内也会换装，cosplay等等。  
- `本插件新增缓存池，缓存池机制，能记录bot换过什么服装并在缓存时间内替换默认服装并保持。  

**行为流程：**

```
源 prompt 进来
  ↓
_resolve_outfit(prompt):
  ├─ 命中具体词（如 连衣裙/汉服/JK）或换装动词（如 换上/今天穿）
  │   ├─ 抽出片段写到缓存（启动 / 刷新 TTL）
  │   └─ 把片段作为 "延续上文穿搭或当前默认服装" 上下文，拼到 prompt 尾部
  ├─ 源 prompt 模糊 → 优先用缓存（TTL 内），回退默认服装
  └─ 啥都没 → no-op
  ↓
effective_prompt（可能含服装上下文后缀）
  ↓
转译 LLM 一起翻译成 SD tags
  ↓
preset + tag → nai.sta1n.cn
```


## 联动插件：astrbot_plugin_private_companion（我会永远陪着你）

本插件可以作为 [`astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion) （"我会永远陪着你"）的联动后端使用。插件启动后会在 `127.0.0.1:8765` 起一个 **OpenAI Images API 兼容** 的本地代理，让陪伴插件把生图请求转发到这里，由 nai.sta1n.cn 完成实际生图。

### 食用方法

在 `astrbot_plugin_private_companion` 的生图设置里：

1. **生图方式** 选择"在线 API 生图"
2. **API 地址** 填写：`http://127.0.0.1:8765/v1`
3. **API Key** 任意填写一个占位符即可（NAI 插件不校验）
4. **模型名** 任意填写一个占位符即可（NAI 插件不校验）

这样配置后，当陪伴插件发出生图请求时，本插件会**自动监听 8765 端口**，接管请求、调用 nai.sta1n.cn 出图，然后把 base64 图片传回陪伴插件。

> 本插件的 `image_gen_key` 仍然要填写真实的 nai.sta1n.cn token，配额和生图参数也是从本插件的 `_conf_schema.json` 读取。  
> 生图密钥可在[千寻寄售](https://www.qianxun1688.com/links/D07F549B)链接中获取平均约0.007/张。  
> 本人与站点和平台没有任何关系，只是作为分享生图平台并不牟利，生图平台也是完全公益。  

---

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

---

> 如果有其他想法可以提 Issue 或 PR。  
