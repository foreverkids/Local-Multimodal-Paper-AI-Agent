import os
import argparse
import shutil
import json
import time
from pypdf import PdfReader
from PIL import Image
import chromadb
from google import genai
from google.genai import types

# =================配置区域=================
GOOGLE_API_KEY = "AIzaSyCHd7JbWSb6q0291vfOwwIHlsjXmzxbl7M"

# 修正了拼写错误并增加默认分类
DEFAULT_TOPICS = "Computer Vision, NLP, Image Deblurring, Operating Systems, Continual Learning, Recommendation Systems"

# 初始化新版 Client
client = genai.Client(api_key=GOOGLE_API_KEY)

# 向量数据库路径
DB_PATH = "./db"

# =================核心功能类=================

class LocalAIAgent:
    def __init__(self):
        # 初始化 ChromaDB
        self.chroma_client = chromadb.PersistentClient(path=DB_PATH)
        self.collection_papers = self.chroma_client.get_or_create_collection(name="papers")
        self.collection_images = self.chroma_client.get_or_create_collection(name="images")

    def _get_embedding(self, text, is_query=False):
        """使用新版 SDK 获取向量"""
        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        try:
            # 新 SDK 嵌入调用方式
            result = client.models.embed_content(
                model="text-embedding-004",
                contents=text,
                config=types.EmbedContentConfig(task_type=task_type)
            )
            return result.embeddings[0].values
        except Exception as e:
            print(f"❌ Embedding Error: {e}")
            return None

    def add_paper(self, file_path, custom_topics=None, retry_count=0):
        if not os.path.exists(file_path): return

        MAX_RETRIES = 2
        print(f"📄 Processing: {os.path.basename(file_path)}...")
        
        # 1. 提取文本
        text_content = ""
        try:
            reader = PdfReader(file_path)
            for page in reader.pages[:3]: 
                text = page.extract_text()
                if text: text_content += text + "\n"
        except Exception as e:
            print(f"❌ PDF Read Error: {e}")
            return

        category = "Uncategorized"
        if len(text_content.strip()) > 50:
            topics = custom_topics if custom_topics else DEFAULT_TOPICS
            prompt = f"Classify this paper into ONE category: [{topics}, Others]. JSON: {{\"category\": \"Name\"}}. Text: {text_content[:5000]}"
            
            # --- 核心修复逻辑：尝试多种模型名称 ---
            # 1.5-flash 在不同版本 SDK 中可能有不同的别名
            model_candidates = ["gemini-2.5-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash-002"]
            
            success = False
            for model_name in model_candidates:
                try:
                    response = client.models.generate_content(
                        model=model_name, 
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json")
                    )
                    res_data = json.loads(response.text)
                    category = res_data.get("category", "Others")
                    print(f"🤖 AI Category: {category} (Model: {model_name})")
                    success = True
                    break # 成功了就跳出循环
                except Exception as e:
                    if "404" in str(e):
                        continue # 404 则尝试下一个名称
                    elif "429" in str(e) and retry_count < MAX_RETRIES:
                        print(f"⏳ Quota reached. Sleeping 10s...")
                        time.sleep(10)
                        return self.add_paper(file_path, custom_topics, retry_count + 1)
                    else:
                        print(f"⚠️ Model {model_name} failed: {e}")
            
            if not success:
                print("❌ All model candidates failed. Attempting to list available models for you...")
                try:
                    # 诊断：打印出你当前 Key 真正支持的所有模型
                    for m in client.models.list():
                        if "generateContent" in m.supported_generation_methods:
                            print(f"   💡 Available model: {m.name}")
                except: pass
                category = "Others"

        # 2. 整理文件 (路径保持和你之前一致)
        base_dir = "./paper"
        target_dir = os.path.join(base_dir, category)
        os.makedirs(target_dir, exist_ok=True)
        new_path = os.path.join(target_dir, os.path.basename(file_path))
        
        try:
            if os.path.abspath(file_path) != os.path.abspath(new_path):
                shutil.move(file_path, new_path)
                print(f"📂 Moved to: {new_path}")
        except: new_path = file_path

        # 3. 向量入库
        try:
            emb = self._get_embedding(text_content[:3000])
            if emb:
                self.collection_papers.add(
                    documents=[text_content[:3000]],
                    embeddings=[emb],
                    metadatas=[{"source": new_path, "category": category}],
                    ids=[new_path]
                )
                print("✅ Indexed.")
        except Exception as e:
            print(f"⚠️ Embedding Error (Possibly Quota): {e}")
    def scan_dir(self, dir_path):
        """
        改进的批量扫描，增加强制延迟
        """
        print(f"🚀 Scanning {dir_path}...")
        pdf_files = []
        for root, _, files in os.walk(dir_path):
            for f in files:
                if f.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, f))
        
        for i, pdf_path in enumerate(pdf_files):
            self.add_paper(pdf_path)
            # 在处理完每一个文件后，强制强制强制休息 10 秒
            # 免费 API 必须佛系处理
            if i < len(pdf_files) - 1:
                print(f"⏳ Cooling down for 7s (File {i+1}/{len(pdf_files)})...")
                time.sleep(7)

    def search_paper(self, query):
        emb = self._get_embedding(query, is_query=True)
        if not emb: return
        results = self.collection_papers.query(query_embeddings=[emb], n_results=3)
        print("\n🔎 Search Results:")
        for i, meta in enumerate(results['metadatas'][0]):
            print(f"{i+1}. [{meta['category']}] {os.path.basename(meta['source'])}")

    def add_image(self, img_path):
        print(f"🖼️ Analyzing image: {img_path}...")
        try:
            img = Image.open(img_path)
            # 新版 SDK 多模态调用
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=["Describe this image for semantic search.", img]
            )
            desc = response.text
            emb = self._get_embedding(desc)
            if emb:
                self.collection_images.add(
                    documents=[desc],
                    embeddings=[emb],
                    metadatas=[{"source": img_path}],
                    ids=[img_path]
                )
                print(f"✅ Image Indexed: {desc[:50]}...")
        except Exception as e:
            print(f"❌ Image Error: {e}")

    def search_image(self, query):
        emb = self._get_embedding(query, is_query=True)
        if not emb: return
        results = self.collection_images.query(query_embeddings=[emb], n_results=3)
        print("\n🖼️ Image Results:")
        for i, meta in enumerate(results['metadatas'][0]):
            print(f"{i+1}. {meta['source']}")

# =================入口=================

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    # 论文相关
    p_add = subparsers.add_parser("add_paper")
    p_add.add_argument("path")
    p_add.add_argument("--topics", default=None)

    p_scan = subparsers.add_parser("scan_dir")
    p_scan.add_argument("path")

    p_search = subparsers.add_parser("search_paper")
    p_search.add_argument("query")

    # 图像相关
    i_add = subparsers.add_parser("add_image")
    i_add.add_argument("path")

    i_search = subparsers.add_parser("search_image")
    i_search.add_argument("query")

    args = parser.parse_args()
    agent = LocalAIAgent()

    if args.command == "add_paper": agent.add_paper(args.path, args.topics)
    elif args.command == "scan_dir": agent.scan_dir(args.path)
    elif args.command == "search_paper": agent.search_paper(args.query)
    elif args.command == "add_image": agent.add_image(args.path)
    elif args.command == "search_image": agent.search_image(args.query)
    else: parser.print_help()

if __name__ == "__main__":
    main()