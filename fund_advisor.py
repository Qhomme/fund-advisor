import os
import re
import time
import requests
import pandas as pd
import akshare as ak
from datetime import datetime

# ==================== 1. 核心自动化参数配置 ====================
# 【请设置环境变量 GEMINI_API_KEY 为您的 Gemini API 密钥】
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Gemini API 代理地址，默认使用 tomdog.cc.cd 代理
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://geminiproxy.tomdog.cc.cd")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# 财联社电报会话（复用 cookie 避免每次重新建立连接）
_cls_session = None


def _get_cls_session():
    """获取或创建财联社请求会话（需先访问页面获取 cookie）"""
    global _cls_session
    if _cls_session is None:
        _cls_session = requests.Session()
        _cls_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        _cls_session.get("https://www.cls.cn/telegraph", timeout=15)
    return _cls_session


def get_cls_telegraph_today():
    """
    获取财联社当日最新电报
    注：/api/cache 为前端轮询缓存，最多返回约 20 条最新电报。
    :return: (标题列表, 内容列表, 时间列表) 三个等长列表
    """
    session = _get_cls_session()
    ts = int(time.time())

    resp = session.get(
        "https://www.cls.cn/api/cache",
        params={"rn": 20, "lastTime": ts, "name": "telegraph"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errno") != 0:
        return [], [], []

    items = data["data"]["roll_data"]
    if not items:
        return [], [], []

    titles = []
    contents = []
    times = []
    for item in reversed(items):
        titles.append(item.get("title", ""))
        contents.append(item.get("content", ""))
        dt = datetime.fromtimestamp(item["ctime"])
        times.append(dt.strftime("%H:%M"))

    return titles, contents, times


def _get_caixin_today_news():
    """获取财新网近两日市场动态新闻（深度分析数量有限）"""
    try:
        df = ak.stock_news_main_cx()
        today = datetime.now()
        items = []
        for _, row in df.iterrows():
            m = re.search(r"/(\d{4}-\d{2}-\d{2})/", row["url"])
            if m:
                d = datetime.strptime(m.group(1), "%Y-%m-%d")
                if (today - d).days <= 1:
                    items.append(f"[财新·{row['tag']}] {row['summary']}")
        return items
    except Exception:
        return []


def _get_eastmoney_news():
    """获取东方财富最新快讯（接口固定10条，不作日期过滤）"""
    try:
        df = ak.stock_news_em()
        items = []
        for _, row in df.iterrows():
            items.append(f"[东财] {row['新闻标题']}")
        return items
    except Exception:
        return []

# 初始策略设定（4:3:1.5:1.5 稳健平衡型新旧科技组合）
PORTFOLIO_STRATEGY = {
    "000628": {"name": "大成高新技术产业股票A", "target_ratio": 0.40, "type": "高端制造/稳健底盘"},
    "512890": {"name": "华泰柏瑞中证红利低波ETF", "target_ratio": 0.30, "type": "红利防守/现金流"},
    "001513": {"name": "易方达信息产业混合A", "target_ratio": 0.15, "type": "纯AI硬件/高弹性进攻"},
    "159819": {"name": "易方达中证人工智能主题ETF", "target_ratio": 0.15, "type": "纯AI指数/高弹性进攻"}
}

# 调仓硬性安全阈值（若实际仓位与目标仓位偏差不超过此比例，优先保持不动，防频繁交易）
REBALANCE_THRESHOLD = 0.05 

# 配置微信号接收端的推手（可选，此处以免费的 PushDeer 为例，不填不影响运行）
PUSHDEER_KEY = os.getenv("PUSHDEER_KEY", "")

# ==================== 2. 数据自动化收集引擎 ====================
def fetch_fund_data():
    """
    第一步：分析当日净值、涨跌幅
    利用 AkShare 实时获取场外基金估值与场内 ETF 当日行情
    """
    print("▶ 正在实时分析组合内基金今日净值与涨跌幅...")
    fund_report = ""

    fund_em_val = pd.DataFrame()
    etf_spot = pd.DataFrame()

    try:
        fund_em_val = ak.fund_value_estimation_em()
    except Exception:
        print("提示：场外估值接口连接较慢，正在启用备用逻辑。")

    try:
        etf_spot = ak.fund_etf_spot_em()
    except Exception:
        print("提示：场内 ETF 行情接口连接较慢，正在启用备用逻辑。")

    for code, info in PORTFOLIO_STRATEGY.items():
        current_mock_ratio = info['target_ratio']

        if code in ["512890", "159819"]:  # 场内 ETF
            if not etf_spot.empty:
                target_row = etf_spot[etf_spot['代码'] == code]
                if not target_row.empty:
                    today_pct = target_row['涨跌幅'].values[0]
                    current_price = target_row['最新价'].values[0]
                    fund_report += f"- 【场内】{info['name']}({code}): 今日最新价: {current_price}, 今日涨跌幅: {today_pct}%, 当前持仓比: {current_mock_ratio*100}%\n"
                else:
                    fund_report += f"- 【场内】{info['name']}({code}): 未找到此 ETF 数据, 当前持仓比: {current_mock_ratio*100}%\n"
            else:
                fund_report += f"- 【场内】{info['name']}({code}): 场内行情暂未刷新, 当前持仓比: {current_mock_ratio*100}%\n"
        else:  # 场外基金
            if not fund_em_val.empty:
                target_row = fund_em_val[fund_em_val['基金代码'] == code]

                if not target_row.empty:
                    pct_col_name = [c for c in target_row.columns if '估算增长率' in c]
                    val_col_name = [c for c in target_row.columns if '估算值' in c]

                    if pct_col_name:
                        today_pct = target_row[pct_col_name[0]].values[0]
                    else:
                        today_pct = "暂无"

                    if val_col_name:
                        gs_time = val_col_name[0].split('-估算数据')[0]
                    else:
                        gs_time = datetime.now().strftime('%Y-%m-%d')

                    fund_report += f"- 【场外】{info['name']}({code}): 今日预估涨跌幅: {today_pct}%, (估值日期: {gs_time}), 当前持仓比: {current_mock_ratio*100}%\n"
                else:
                    fund_report += f"- 【场外】{info['name']}({code}): 估值数据暂无, 当前持仓比: {current_mock_ratio*100}%\n"
            else:
                fund_report += f"- 【场外】{info['name']}({code}): 估值接口不可用, 当前持仓比: {current_mock_ratio*100}%\n"
    return fund_report

def fetch_portfolio_holdings():
    """
    获取组合基金最新季报的前十大重仓股，用于持仓变动分析
    """
    print("▶ 正在分析基金最新季报持仓...")
    report = ""

    # ETF 是指数基金，不披露主动持仓，此处仅展示场外主动基金
    active_funds = {k: v for k, v in PORTFOLIO_STRATEGY.items() if k not in ["512890", "159819"]}

    for code, info in active_funds.items():
        try:
            df = ak.fund_portfolio_hold_em(symbol=code, date="2026")
            if df.empty:
                report += f"- {info['name']}({code}): 暂无最新持仓数据\n"
                continue
            latest_q = df["季度"].iloc[0]
            report += f"- {info['name']}({code})  [{latest_q}]:\n"
            for _, row in df.head(5).iterrows():
                report += f"    {row['股票名称']}({row['股票代码']}) 占比{row['占净值比例']}%\n"
        except Exception:
            report += f"- {info['name']}({code}): 持仓数据获取失败\n"

    return report if report else "暂无持仓数据。"


def fetch_market_news():
    """
    第二步：多源聚合当日重要新闻
    来源：财联社电报 + 财新网 + 东方财富
    """
    print("▶ 正在多源聚合今日重要财经新闻...")
    all_news = []

    # 1. 财联社电报（实时快讯，最新 20 条）
    try:
        titles, contents, times = get_cls_telegraph_today()
        if titles:
            for t, title, content in zip(times, titles, contents):
                text = title if title else content[:80]
                all_news.append(f"[财联社 {t}] {text}")
            print(f"  财联社电报: {len(titles)} 条")
    except Exception as e:
        print(f"  财联社获取失败: {e}")

    # 2. 财新网（深度市场分析）
    caixin = _get_caixin_today_news()
    if caixin:
        all_news.extend(caixin)
        print(f"  财新网: {len(caixin)} 条")

    # 3. 东方财富（个股/市场快讯）
    em_news = _get_eastmoney_news()
    if em_news:
        all_news.extend(em_news)
        print(f"  东方财富: {len(em_news)} 条")

    if not all_news:
        return "今日暂无重要财经新闻。"

    print(f"  总计: {len(all_news)} 条")
    return "\n".join(all_news)

# ==================== 3. Gemini 智能决策大脑 ====================
def ask_gemini_advisor(fund_data, market_news, portfolio_holdings):
    """
    第三步：通过 OpenAI 兼容代理调用 Gemini，给出调仓建议
    """
    print("▶ 正在将数据移交 Gemini 智能理财经理进行全盘量化审计...")

    prompt = f"""
您现在是我的私人顶级资产配置专家，持有特许金融分析师（CFA）及高级理财经理资格。
我的总资产配置策略是【70%稳健（大成制造+红利低波）+ 30%纯AI科技（易方达+人工智能）】。

官方标准标的及比例硬性设定如下：
- 大成高新技术产业股票A (000628) —— 目标占比 40% (底盘)
- 华泰柏瑞中证红利低波ETF (512890) —— 目标占比 30% (防守)
- 易方达信息产业混合A (001513) —— 目标占比 15% (进攻)
- 易方达中证人工智能主题ETF (159819) —— 目标占比 15% (进攻)

以下是系统刚刚抓取的今日最新数据：

【组合基金当日表现及当前仓位】
{fund_data}

【最新市场宏观电报快讯】
{market_news}

【基金最新季报重仓股持仓】
{portfolio_holdings}

请严格遵循以下流程给出专业决策报告：
1. 【第一行必须有且仅有明确结论】：格式必须为"今日决策：保持观望，不作调整" 或 "今日决策：执行调仓"。
2. 【数据审计】：计算各持仓今日波动后是否偏离了硬性目标比例。如果未遭遇大面积暴跌、或者偏离度未超过安全阈值（5%），请务必坚守长线纪律，优先选择"保持观望"。
3. 【持仓诊断】：检查各基金最新季报的重仓股是否与基金定位一致。如果发现重仓股行业漂移、集中度过高、或持仓风格与策略相悖，请在报告中指出风险。
4. 【宏观风险评估】：结合快讯，重点诊断【美伊冲突升级对全球算力/供应链的情绪传导】、以及【中国AI硬件出口与传统制造去产能】在今日是否有突发系统性风险。
5. 【市面竞品与调仓精细化建议】：
   - 若需要调仓：明确列出"卖出XX基金百分之几，买入XX基金百分之几"。
   - 若保持不调整：请另外推荐1支当前市面上极具潜力的其它类型基金（如跨境纳指QDII、或清洁能源REITs），并说明其是否适合作为下一阶段的备用弹药。

语言要求：语气要像严谨、落地、绝不忽悠的专业私人理财经理，不讲空话，字数控制在400字以内，重点字词使用加粗符号强化阅读。
"""

    try:
        resp = requests.post(
            f"{GEMINI_BASE_URL}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GEMINI_API_KEY}",
            },
            json={
                "model": GEMINI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        return f"Gemini 代理调用失败，网络错误: {str(e)}"
    except (KeyError, IndexError) as e:
        return f"Gemini 代理返回格式异常: {str(e)}, 原始响应: {resp.text[:200]}"

# ==================== 4. 手机端自动化推送模块 ====================
def push_report_to_mobile(content):
    """改用 Server酱 接口，消息将直接发送到你的【微信扣扣/方糖服务号】"""
    SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "")
    
    if not SERVER_CHAN_KEY:
        print("\n[未配置Key，控制台打印]：\n", content)
        return
        
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    data = {
        "title": "📊 每日AI理财决策报告",
        "desp": content # 完美支持大模型生成的 markdown 格式
    }
    try:
        requests.post(url, data=data, timeout=30)
        print("▶ 报告已成功通过【Server酱】发送至您的个人微信！")
    except Exception:
        print("微信推送失败，请检查网络。")


# ==================== 5. 系统主入口 ====================
if __name__ == "__main__":
    print(f"=== 基金自动化诊断系统启动 | 当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    if not GEMINI_API_KEY:
        print("错误：未设置 GEMINI_API_KEY 环境变量，请先配置 Gemini API 密钥。")
        print("示例: export GEMINI_API_KEY='your-api-key'")
        exit(1)

    fund_info = fetch_fund_data()
    market_news = fetch_market_news()
    portfolio_holdings = fetch_portfolio_holdings()
    final_advice = ask_gemini_advisor(fund_info, market_news, portfolio_holdings)
    push_report_to_mobile(final_advice)

    print("=== 今日诊断审计流结束 ===")
