# astrbot_plugin_jm_bot

## 所有内容均由gpt-5.4生成 不保证功能的可用性 请自行参考

适配 AstrBot 的 JM 漫画搜索、选章、单图提取与 PDF 发送插件。

这个插件围绕 `搜jm` / `看jm` 两条主链路设计：
- `搜jm` 负责搜索、封面缩略图预览、结果缓存、页数补全与翻页提示
- `看jm` 负责查看详情、章节选择、多章节下载、单图提取与 PDF 发送

同时，插件针对 AstrBot 场景做了几轮实战优化：
- 命令执行后阻断 LLM 误触发
- 转发消息与纯文本消息支持自动撤回
- `jm更新域名`、`jm清空域名` 默认管理员执行，可在行为管理中调整
- 搜索结果支持封面拼图与转发消息发送
- 多章节详情支持受控并发补全章节标题和页数
- PDF 生成移入后台线程，并通过串行锁避免阻塞 AstrBot 主事件处理
- 下载目录、缓存目录、临时目录支持统一清理，便于发布后长期运行

## 项目地址

- GitHub: `https://github.com/Huac233/astrbot_plugin_jm_bot`

## 致谢与参考

本项目的设计与落地过程参考了以下项目与资料：

- AstrBot 插件模板：`https://github.com/Soulter/helloworld`
- AstrBot EH 插件：`https://github.com/drdon1234/astrbot_plugin_ehentai_bot`
- ShowMeJM：`https://github.com/exneverbur/ShowMeJM`
- JMComic-Crawler-Python：`https://github.com/hect0x7/JMComic-Crawler-Python`
- AstrBot 开发文档：`https://docs.astrbot.app/dev/star/guides/listen-message-event.html`

其中，搜索结果封面预览、转发消息发送体验、命令行为管理适配等思路，重点参考了 AstrBot 插件生态的现有实现；JM 下载与接口调用能力则主要建立在 `jmcomic` 生态之上。

## 功能特性

### 1. 搜索与预览

- 支持 `搜jm [关键词] [页码]`
- 搜索结果以转发消息发送
- 支持封面缩略图拼图预览
- 支持前若干条结果补全作品总页数 `[XXP]`
- 支持每个用户独立搜索缓存，后续可直接 `看jm [序号]`
- 支持翻页提示，如 `搜jm 关键词 2`

### 2. 查看与下载

- 支持 `看jm [序号或id]`
- 单章节作品可直接下载
- 多章节作品会先返回章节列表，再选择章节下载
- 支持 `看jm [序号或id] [章节]`
- 支持多章节选择，例如：`看jm 1 1,3,5-7`
- 选中多个章节时，会并发补全章节标题和页数信息

### 3. 单图提取

- 支持 `看jm [序号或id] [章节] P[页码]`
- 仅提取指定章节中的单张图片
- 不走整章 PDF 打包，更适合快速取图

### 4. 域名管理

- 支持 `jm更新域名`
- 支持 `jm清空域名`
- 默认管理员权限执行
- 可在 AstrBot 行为管理中改为所有人可执行

### 5. 自动撤回

- 纯文本消息支持自动撤回
- 转发消息支持自动撤回（在支持 `message_id` 的发送链路下）
- 图片、PDF、文件默认不撤回
- 可通过配置控制秒数，默认 60 秒，填 `0` 关闭

### 6. 缓存与清理

- 搜索缓存支持 TTL
- 章节选择缓存支持 TTL
- 页数补全支持内存缓存
- 封面缓存支持最大文件数淘汰
- 支持统一清理下载目录、封面缓存、缓存文件、运行时临时文件

## 安装方式

### 方式一：通过 GitHub 仓库安装

在 AstrBot WebUI 插件市场中，使用仓库地址安装：

```text
https://github.com/Huac233/astrbot_plugin_jm_bot
```

### 方式二：本地插件目录安装

将本项目放入 AstrBot 的插件目录后，重载插件即可。

推荐目录：

```text
/AstrBot/data/plugins/astrbot_plugin_jm_bot
```

## Docker 使用注意事项

如果你使用 Docker 部署，并且 `AstrBot` 与 `NapCat` 运行在不同容器中，需要特别注意文件路径互通问题。

这个插件在发送图片、PDF、转发中的图片节点时，通常会先在 AstrBot 容器内生成本地文件，再交给消息侧读取并发送。
如果 `NapCat` 无法访问这些文件路径，就可能出现以下现象：

- 图片或 PDF 明明已经生成，但发送失败
- 转发消息中的图片无法显示
- AstrBot 容器内路径存在，但 NapCat 容器内路径不存在

因此请确保：

- `AstrBot` 与 `NapCat` 看到的是同一份实际文件
- 两边容器中的挂载目录能够互相对应
- 如果路径不同，需要通过软链接、相同挂载点或其他映射方式打通

例如你当前的环境里，就需要注意这类路径是否实际指向同一份数据：

```text
/opt/1panel/apps/astrbot/astrbot/data ##AstrBot挂载卷
/AstrBot/data
```

如果这两个路径在不同容器里不能互相访问，那么插件即使已经成功生成文件，消息侧也仍然可能拿不到文件。

## 依赖

插件依赖写在 `requirements.txt` 中，当前仓库内声明的运行依赖包括：

- `jmcomic`
- `Pillow`
- `pikepdf`
- `PyYAML`
- `aiofiles`
- `curl-cffi`

运行依赖请以 `requirements.txt` 为准。

如果你的运行环境无法自动安装依赖，请手动安装。

## 使用说明

### 指令总览

- `搜jm [关键词] [页码]`
- `看jm [序号或id]`
- `看jm [序号或id] [章节编号/范围]`
- `看jm [序号或id] [章节] P[页码]`
- `随机jm [关键词]`
- `jm更新域名`
- `jm清空域名`
- `jm清理缓存`

### 搜索示例

```text
搜jm 萝莉
搜jm 韩漫 2
搜jm 萝莉 +无修正 -AI
```

### 查看与下载示例

```text
看jm 1
看jm 1356854
看jm 1 2
看jm 1 1,3,5-7
```

### 单图示例

```text
看jm 8 2 P5
```

### 随机示例

```text
随机jm
随机jm 萝莉
```

## 配置说明

插件启动后会自动整理 `config.yaml`。推荐优先通过 AstrBot WebUI 的插件配置面板修改配置。

### 请求相关

- `request.enabled`
  - 是否启用代理
- `request.proxies`
  - 代理地址，例如：`http://mihomo:7890`
- `request.timeout`
  - 请求超时秒数
- `request.max_retries`
  - 请求重试次数

### 输出相关

- `output.base_dir`
  - 下载输出目录
- `output.pdf_max_pages`
  - 单个 PDF 最大页数，超过会自动拆卷
- `output.jpeg_quality`
  - 图片转 PDF 时的图片质量
- `output.pdf_password`
  - PDF 密码，可空
- `output.max_local_albums`
  - 本地最多保留漫画目录数
- `output.max_local_chapters`
  - 单本漫画最多保留章节目录数
- `output.cover_cache_dir`
  - 搜索封面缓存目录
- `output.cover_cache_max_files`
  - 封面缓存最大保留文件数

### 下载并发

- `download.image_threads`
  - 单章节图片下载并发
- `download.photo_threads`
  - 整本/多章节下载并发

### 交互相关

- `interaction.chapter_fold_threshold`
  - 章节列表折叠阈值
- `interaction.max_download_images`
  - 单次允许下载的最大图片数
- `interaction.max_download_chapters`
  - 单次允许下载的最大章节数
- `interaction.search_page_count_threads`
  - 搜索结果补 P 数并发
- `interaction.search_cover_threads`
  - 搜索页封面下载并发
- `interaction.chapter_detail_threads`
  - 多章节详情补全并发
- `interaction.chapter_selection_ttl`
  - 章节选择缓存保留秒数
- `interaction.auto_recall_seconds`
  - 纯文本 / 转发消息自动撤回秒数，默认 60，`0` 为关闭

### 功能开关

- `features.open_random_search`
  - 是否启用 `随机jm`

### 可改命令别名

以下命令支持通过配置改主命令名：

- `commands.search`
- `commands.view`
- `commands.random`
- `commands.update_domain`
- `commands.clear_domain`

插件启动时会将配置中的命令名绑定到运行时命令，同时保留默认命令作为别名，方便平滑迁移与行为管理调整。

## 行为管理说明

- `jm更新域名`
- `jm清空域名`

这两条命令默认使用管理员权限过滤。

如果你希望开放给普通用户执行，可以直接在 AstrBot 的行为管理中调整，不需要修改代码。

## 目录与缓存说明

默认情况下，插件会使用类似下面的目录结构：

```text
plugin_data/
└── astrbot_plugin_jm_bot/
    ├── download/
    │   └── [album_id]/
    ├── cover_cache/
    ├── search_cache.json
    ├── chapter_selection_cache.json
    ├── jm_max_page.json
    └── jm_option.yml
```

`jm清理缓存` 会尽量清理以下内容：

- 下载目录中的漫画目录
- 下载目录中的散文件
- 封面缓存文件
- 搜索缓存 / 随机缓存 / 章节选择缓存
- `jm_option.yml`
- 插件根目录下遗留的临时目录与中间文件

这部分已经按长期运行场景补过一轮，适合作为发布版使用。

## 代理说明

如果你的环境需要代理访问 JM，请在配置中填写：

```text
request.proxies = http://mihomo:7890
```

插件会同时把代理注入到运行环境和 `jmcomic` 选项文件中。

## 已知说明

- 搜索结果页补 P 数本质上仍需要额外接口调用；并发可调，但调太高会增加站点和代理压力
- PDF 生成已经移入后台线程，并通过串行锁避免同时大量构建导致 AstrBot 卡顿
- 图片、PDF、文件默认不参与自动撤回，这是有意保留的安全边界

## 发布建议

发布前建议至少手动验证下面几项：

- `搜jm 萝莉 1`
- `看jm [序号]`
- `看jm [序号] [章节]`
- `看jm [序号] [章节] P[页码]`
- `随机jm`
- `jm更新域名`
- `jm清空域名`
- `jm清理缓存`

同时确认：

- 命令执行后不会误触发 LLM
- 转发消息可正常发送
- 自动撤回按配置生效
- 多章节详情补全正常
- 清理命令能清掉运行时残留目录与文件

## 免责声明

本插件仅用于技术研究、接口调试与个人学习，请遵守你所在地区的法律法规，以及目标站点的使用条款。
请勿将本插件用于违规传播、批量搬运或其他不当用途。
