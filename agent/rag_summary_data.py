import json
import os
import asyncio
from tqdm.asyncio import tqdm
from openai import AsyncOpenAI
from dotenv import load_dotenv

try:
    from .config import RAG_CHECKPOINT_FILE, RAG_SOURCE_FILE, RAG_SUMMARY_FILE
except ImportError:  # Allows: python agent/rag_summary_data.py
    from config import RAG_CHECKPOINT_FILE, RAG_SOURCE_FILE, RAG_SUMMARY_FILE

load_dotenv()

API_CONFIG = {
    "api_key": os.getenv("DEEPSEEK_API_KEY"),
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "max_workers": 200,  # 异步并发数
    "rate_limit": 200,  # 每分钟请求限制，0=不限制
}

INPUT_FILE = RAG_SOURCE_FILE
OUTPUT_FILE = RAG_SUMMARY_FILE
CHECKPOINT_FILE = RAG_CHECKPOINT_FILE  # 记录已处理的 id 集合

SYSTEM_PROMPT = """你是一名专业的健康知识提炼助手。你的任务是根据用户提出的健康问题，从健康管理专家的回答中提炼出一段精炼的RAG参考知识。

【提炼要求】
1. 提取回答中的核心健康知识、调理建议和注意事项
2. 去除第一人称（"我"、"我们"）和面向当前用户的个性化表述，改为客观陈述
3. 保留关键的健康术语、药物名称、检查手段、饮食运动建议等
4. 提炼结果应该是一段独立、完整、自洽的参考文本，可供后续检索使用
5. 字数控制在250字以内

【输出要求】
只输出提炼后的参考文本，不要任何前缀后缀或解释。"""

USER_PROMPT_TEMPLATE = """【用户问题】
{query}

【健康管理专家回答】
{reply}

请提炼为RAG参考知识："""


def get_user_prompt(query, reply):
    max_length = 3000
    query = query[:max_length]
    reply = reply[:max_length]
    return USER_PROMPT_TEMPLATE.format(query=query, reply=reply)


async def process_single(args, client: AsyncOpenAI, sem: asyncio.Semaphore):
    index, data = args
    user_query = data.get("user_query", "")
    assistant_reply = data.get("assistant_reply", "")

    async with sem:
        for attempt in range(3):
            try:
                completion = await client.chat.completions.create(
                    model=API_CONFIG["model"],
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": get_user_prompt(user_query, assistant_reply)},
                    ],
                    temperature=0.5,
                    max_tokens=800,
                    timeout=60,
                )
                summary = completion.choices[0].message.content.strip()
                new_item = {
                    "id": data.get("id", ""),
                    "user_query": user_query,
                    "assistant_reply": assistant_reply,
                    "rag_summary": summary,
                    "expected_intent": data.get("expected_intent", "")
                }
                return index, new_item, True
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    new_item = {
                        "id": data.get("id", ""),
                        "user_query": user_query,
                        "assistant_reply": assistant_reply,
                        "rag_summary": "",
                        "expected_intent": data.get("expected_intent", "")
                    }
                    return index, new_item, False
    return index, new_item, False


# ========== 断点续传 ==========

def load_checkpoint():
    """加载已处理的 id 集合（统一为字符串类型）"""
    if os.path.exists(CHECKPOINT_FILE):
        processed = set()
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    processed.add(line)
        return processed

    # 首次运行：从输出文件扫描已处理的 id
    processed = set()
    if os.path.exists(OUTPUT_FILE):
        print(f"🔄 首次运行，从输出文件扫描已处理记录...")
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        item = json.loads(line)
                        item_id = str(item.get("id", ""))
                        if item_id:
                            processed.add(item_id)
                    except json.JSONDecodeError:
                        pass
        if processed:
            save_checkpoint_ids(list(processed))
        print(f"   扫描到 {len(processed)} 条已处理记录")
    return processed


def save_checkpoint_ids(id_list):
    """批量追加写入 id 到检查点文件"""
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        for id_val in id_list:
            f.write(f"{id_val}\n")


def get_id(item):
    """统一获取字符串形式的 id，消除 int/str 类型不一致"""
    return str(item.get("id", ""))


async def main():
    if not API_CONFIG["api_key"]:
        print("❌ 请设置环境变量 DEEPSEEK_API_KEY")
        return

    # 加载检查点
    processed_ids = load_checkpoint()
    print(f"📂 加载数据...")
    data_list = []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                data_list.append(json.loads(line))

    # 过滤已处理的（统一转为字符串比较，消除 int/str 类型不一致）
    pending = [(i, d) for i, d in enumerate(data_list) if get_id(d) not in processed_ids]
    total = len(data_list)
    pending_count = len(pending)
    print(f"   共 {total} 条记录，已处理 {total - pending_count} 条，待处理 {pending_count} 条")

    if pending_count == 0:
        print("✅ 所有数据已处理完毕！")
        return

    # 确保输出文件存在
    if not os.path.exists(OUTPUT_FILE):
        open(OUTPUT_FILE, "w", encoding="utf-8").close()

    client = AsyncOpenAI(api_key=API_CONFIG["api_key"], base_url=API_CONFIG["base_url"])
    sem = asyncio.Semaphore(API_CONFIG["max_workers"])
    write_lock = asyncio.Lock()

    success_count = 0
    fail_count = 0
    checkpoint_buffer = []
    checkpoint_interval = 50  # 每处理 50 条写入一次检查点

    async def worker(args):
        nonlocal success_count, fail_count, checkpoint_buffer
        index, new_item, ok = await process_single(args, client, sem)
        data_id = get_id(new_item)
        async with write_lock:
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(new_item, ensure_ascii=False) + "\n")
            checkpoint_buffer.append(data_id)
            if len(checkpoint_buffer) >= checkpoint_interval:
                save_checkpoint_ids(checkpoint_buffer)
                checkpoint_buffer.clear()
            if ok:
                success_count += 1
            else:
                fail_count += 1
            pbar.update(1)
            pbar.set_postfix({"成功": success_count, "失败": fail_count, "待处理": pending_count - success_count - fail_count})

    pbar = tqdm(total=pending_count, desc="提炼进度")
    tasks = [asyncio.create_task(worker(item)) for item in pending]
    await asyncio.gather(*tasks)
    pbar.close()

    # 兜底：保存剩余的检查点
    if checkpoint_buffer:
        save_checkpoint_ids(checkpoint_buffer)

    print(f"\n✅ 完成！成功: {success_count}, 失败: {fail_count}")
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("   已清理检查点文件")


if __name__ == "__main__":
    asyncio.run(main())
