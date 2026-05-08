# 隔夜选股AI分析系统

A股隔夜选股分析系统，使用AI（DeepSeek）进行实时分析，部署在阿里云函数计算(FC)上。

## 🚀 快速开始

### 本地测试（5分钟）

```bash
# 1. 安装依赖
pip install -r requirement.txt

# 2. 运行本地测试
python test_local.py

# 结果：生成HTML报告到 /tmp/reports/
```

### 部署到阿里云（30分钟）

```bash
# 1. 安装部署工具
pip install funcraft

# 2. 配置阿里云
fun config

# 3. 修改template.yml
# - ACCOUNT_ID: 你的阿里云账户ID
# - OSS_ACCESS_KEY, OSS_SECRET_KEY: 你的访问密钥

# 4. 部署
fun deploy

# 完成！每个工作日14:00会自动执行
```

## 📖 文档导航

| 文档 | 说明 |
|------|------|
| **部署步骤.txt** | ⭐ 中文快速指南（推荐首先阅读） |
| **改造总结.md** | 项目改造说明和架构设计 |
| **DEPLOY_GUIDE.md** | 详细部署指南（英文版） |

## ✨ 核心特性

- 🤖 **AI分析** - 使用DeepSeek V3进行股票评分
- 📊 **数据采集** - Tushare + AkShare实时行情
- 📄 **报告生成** - 漂亮的HTML静态报告
- ☁️ **云端部署** - 阿里云FC Serverless
- 📱 **手机访问** - 通过OSS公网URL访问
- ⏰ **自动定时** - 工作日14:00自动执行
- 💰 **成本极低** - 月费<1元

## 🏗️ 项目结构

```
aibot/
├── fc_main.py                 # 阿里云FC入口函数
├── main.py                    # 本地定时任务脚本
├── data_collector.py          # 数据采集模块
├── ai_analyzer.py             # AI分析模块
├── report_generator.py        # 报告生成模块
├── oss_uploader.py            # OSS上传模块
├── config.py                  # 配置文件
│
├── test_local.py              # 本地测试脚本
├── template.yml               # FC部署配置
│
├── 部署步骤.txt               # 中文快速指南
├── 改造总结.md                # 项目改造说明
├── DEPLOY_GUIDE.md            # 详细部署指南
└── requirement.txt            # Python依赖
```

## 🔧 配置说明

### API密钥配置

编辑 `config.py` 或设置环境变量：

```python
# Tushare Token - https://tushare.pro/
TUSHARE_TOKEN = 'your_token'

# Silicon API Key - 硅基流动（免费额度较大）
SILICON_API_KEY = 'your_key'

# 阿里云OSS配置
OSS_ENDPOINT = 'oss-cn-beijing.aliyuncs.com'  # 改成你的地域
OSS_BUCKET = 'stockbao'
OSS_ACCESS_KEY = 'your_access_key'
OSS_SECRET_KEY = 'your_secret_key'
OSS_DOMAIN = 'stockbao.oss-cn-beijing.aliyuncs.com'  # 公网访问域名
```

## 💻 命令参考

### 本地运行

```bash
# 立即执行一次分析
python main.py now

# 启动定时任务（后台运行）
python main.py

# 本地测试（前3只股票）
python test_local.py
```

### FC部署

```bash
# 初始化配置
fun config

# 部署到阿里云
fun deploy

# 查看函数信息
fun info stockbao-service stock-analysis-schedule

# 查看执行日志
fun logs -s stockbao-service -f stock-analysis-schedule

# 远程测试
fun invoke -s stockbao-service -f stock-analysis-schedule
```

## 📱 访问报告

部署完成后，报告保存在OSS，可通过以下URL访问：

```
https://YOUR_BUCKET.oss-cn-REGION.aliyuncs.com/stock_reports/latest.html
```

用手机浏览器打开，即可查看最新的选股报告。

## 🧪 故障排查

| 问题 | 解决方案 |
|------|---------|
| 本地测试报错 | 检查API密钥是否有效，依赖是否完整 |
| FC部署失败 | 检查template.yml配置，确保ACCOUNT_ID正确 |
| OSS上传失败 | 检查AccessKey、SecretKey、Bucket权限 |
| 函数超时 | 增加FC超时时间或减少分析的股票数量 |
| 数据采集失败 | 检查API配额，网络连接 |

## 📚 技术栈

- **数据采集**: Tushare, AkShare
- **AI分析**: DeepSeek V3 (via Silicon Flow API)
- **云平台**: Aliyun FC (函数计算), OSS (对象存储)
- **语言**: Python 3.9+
- **框架**: asyncio, httpx, oss2

## 💡 常见问题

**Q: 为什么选择FC而不是ECS?**  
A: FC是Serverless架构，按使用计费（极便宜），无需管理服务器，自动扩展。

**Q: 报告可以保存多长时间?**  
A: 在OSS上永久保存，可配置生命周期规则自动删除旧文件。

**Q: 能否改成其他时间执行?**  
A: 可以，修改template.yml中的CronExpression即可。

**Q: 能否在本地一直运行?**  
A: 可以，执行 `python main.py` 即可启动定时任务。

## 🎯 下一步

1. 修改template.yml中的密钥信息
2. 运行 `python test_local.py` 验证本地环境
3. 执行 `fun deploy` 部署到阿里云
4. 等待工作日14:00自动执行
5. 通过OSS公网URL在手机上访问报告

## 📞 技术支持

- 查看详细部署指南: `DEPLOY_GUIDE.md`
- 查看改造说明: `改造总结.md`
- 查看代码注释: 各个.py文件

## 📄 License

自用代码，欢迎学习和参考。

---

**最后更新**: 2025-12-14  
**状态**: ✅ 可直接部署  
**成本**: 💰 月费 < 1元
