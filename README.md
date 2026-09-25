# Zotero PDF Harvester

面向 Zotero 的批量 PDF 补全工具：从合法开放获取来源搜索全文，下载成功后自动作为附件回挂到原文献条目。项目可缓存、可重复运行，已经有 PDF 的条目会跳过。

> 本项目不使用 Sci-Hub，也不绕过付费墙。机构浏览器模式只复用你本人已获授权的学校/机构订阅。

## 主要功能

- 直接读取一个或多个 Zotero 分类，不需要导出 CSV。
- 并行查询 Unpaywall、OpenAlex、OpenAIRE、Europe PMC/PMC、Crossref、Semantic Scholar、NCBI OA；配置密钥后额外查询 CORE。
- 可选调用用户本人授权的 Elsevier TDM、Wiley TDM；只接受完整 `%PDF`，Elsevier 的“仅第一页”响应会被拒绝。
- 自动调用 `fetchpdf` 做更广的开放获取兜底。
- 可选 Playwright 机构浏览器兜底，复用本机登录状态处理有合法订阅权限的长尾出版商。
- 下载成功后立即回挂 Zotero，意外中断后可安全重跑。
- 短网络超时、单 DOI 硬超时和磁盘缓存，避免一个慢站点拖死整批任务。
- 输出 JSON 报告，保留未命中和失败原因。

## 新电脑一键安装

### macOS / Linux

先安装 [Python 3.10+](https://www.python.org/downloads/) 和 Git，然后：

```bash
git clone https://github.com/wzs2004/zotero-pdf-harvester.git
cd zotero-pdf-harvester
./install.sh
```

安装脚本会自动创建 `.venv`，安装本项目、`fetchpdf`、机构浏览器组件及 Chromium，并复制 `.env.example` 为 `.env`。

### Windows PowerShell

```powershell
git clone https://github.com/wzs2004/zotero-pdf-harvester.git
cd zotero-pdf-harvester
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

## 首次配置

1. 编辑 `.env`，至少填写：

   ```dotenv
   ZPH_EMAIL=you@example.com
   ```

   邮箱仅用于 Unpaywall、Crossref 等公开学术 API 的礼貌访问标识。

2. 启动 Zotero，在“设置 → 高级”中启用“允许其他应用程序与 Zotero 通信”。
3. 第一次运行时 Zotero 会弹出本地授权框，请选择“始终允许 / Always Allow”。密钥只存放在本机用户配置目录，不会写入仓库。

CORE、OpenAlex、Semantic Scholar、NCBI、Elsevier、Wiley、Springer 的 API 密钥都是可选项，可在 `.env` 中配置以提高覆盖率或降低限速。Elsevier/Wiley 密钥必须来自你本人合法注册或机构授权，不会绕过付费墙。不要提交 `.env`。

## 使用

快速公开来源 + `fetchpdf` 兜底：

```bash
./run.sh --collection '楔状缺损_NCCL_有限元'
./run.sh --collection lys
```

一次处理多个分类：

```bash
./run.sh \
  --collection '楔状缺损_NCCL_有限元' \
  --collection lys \
  --workers 20
```

使用学校/机构订阅兜底：

```bash
./run.sh --collection lys --institutional-browser
```

第一次使用机构模式时会打开 Chromium。完成学校 SSO 登录后，Cookie 保存在本机浏览器 profile 中，后续运行会复用。该模式只对你所在机构确实订阅的内容有效，而且为避免同一 profile 并发损坏，会串行处理最后的长尾条目，因此只建议对快速模式未命中的条目使用。

自定义输出和报告：

```bash
./run.sh \
  --collection lys \
  --output downloads \
  --report reports/lys.json \
  --timeout 18 \
  --fallback-timeout 40
```

完整参数：

```bash
./run.sh --help
```

## 获取链路

```text
Zotero 分类
  ├─ 已有 PDF → 跳过
  └─ 无 PDF
      ├─ Unpaywall / OpenAlex / Europe PMC / PMC
      ├─ OpenAIRE 仓储发现 / Crossref / Semantic Scholar / NCBI OA / CORE（可选密钥）
      ├─ Elsevier TDM / Wiley TDM（可选、需本人授权密钥）
      ├─ fetchpdf 多来源兜底
      └─ 机构浏览器（可选，本人合法订阅）
           ↓
        验证 PDF → 自动回挂 Zotero → JSON 报告
```

未找到 PDF 不一定是程序故障，常见原因包括：没有公开全文、只有订阅版本、机构没有订阅、出版社反自动化验证、元数据缺少 DOI，或远端服务暂时限速。

## 为什么比逐条浏览器搜索快

- 元数据来源并行查询；候选 PDF 一旦成功立即停止。
- 快速公开来源先跑，较慢的 `fetchpdf` 和机构浏览器只处理长尾。
- 单个候选 URL、单个 DOI 都有明确超时。
- 已有附件与本地缓存不会重复下载。
- 机构浏览器串行运行，避免多个进程争用同一个登录 profile。

## 测试

```bash
.venv/bin/python -m pytest -q
```

## 借鉴与依赖

- [The-Metascience-Observatory/fetchpdf](https://github.com/The-Metascience-Observatory/fetchpdf)：多来源 OA、仓储和出版社兜底。
- [jxtse/auto-paper-harvester](https://github.com/jxtse/auto-paper-harvester)：出版商路由、TDM API 和机构浏览器会话。
- [OpenAlex](https://github.com/ourresearch/openalex-official)：开放学术元数据和 OA 地址。

## 隐私与合规

- 不提交 Zotero 本地密钥、API 密钥、邮箱、机构 Cookie 或浏览器 profile。
- 只下载开放获取内容，或你本人已有合法访问权限的内容。
- 请遵守出版商、机构和 API 的服务条款及频率限制。

## License

MIT
