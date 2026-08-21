"""
LLM 分析引擎 - 支持 LM Studio / Ollama / OpenAI

优先级：LM Studio > Ollama > OpenAI
推荐：LM Studio（图形界面友好，OpenAI兼容API）
"""
import os
import json
import hashlib
import difflib
import re
import urllib.request
import urllib.error
import ssl
import subprocess
from urllib.parse import urljoin, urlparse
from html.parser import HTMLParser

import requests


_WEBSITE_SSL_CONTEXT = None


def _website_ssl_context():
    """Return a verified TLS context that also trusts Windows certificate stores.

    Python's bundled OpenSSL trust list can differ from the certificates trusted
    by browsers on Windows.  Importing the Windows ROOT and CA stores keeps HTTPS
    verification enabled while allowing sites that use an organisation-approved
    issuer to be read by the CRM as well.
    """
    global _WEBSITE_SSL_CONTEXT
    if _WEBSITE_SSL_CONTEXT is not None:
        return _WEBSITE_SSL_CONTEXT

    context = ssl.create_default_context()
    if os.name == 'nt' and hasattr(ssl, 'enum_certificates'):
        server_auth_oid = ssl.Purpose.SERVER_AUTH.oid
        for store_name in ('ROOT', 'CA'):
            try:
                certificates = ssl.enum_certificates(store_name)
            except OSError:
                continue
            for certificate, encoding, trust in certificates:
                if encoding != 'x509_asn':
                    continue
                if trust is not True and server_auth_oid not in trust:
                    continue
                try:
                    context.load_verify_locations(
                        cadata=ssl.DER_cert_to_PEM_cert(certificate)
                    )
                except ssl.SSLError:
                    # A malformed or unsupported system-store entry must not
                    # prevent other valid issuer certificates from loading.
                    continue

    _WEBSITE_SSL_CONTEXT = context
    return context


def _read_with_windows_curl(url: str, timeout: int) -> str:
    """Read an HTTPS page through Windows Schannel while retaining TLS checks."""
    if os.name != 'nt':
        raise OSError('Windows Schannel is unavailable')
    completed = subprocess.run(
        [
            'curl.exe', '--fail', '--location', '--silent', '--show-error',
            '--compressed', '--max-time', str(timeout), '--connect-timeout', str(min(timeout, 10)),
            '--user-agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '--header', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            '--header', 'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8',
            '--header', 'Upgrade-Insecure-Requests: 1',
            url,
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        error = completed.stderr.decode('utf-8', errors='replace').strip()
        raise urllib.error.URLError(error or 'Windows TLS request failed')
    return completed.stdout.decode('utf-8', errors='replace')


def _read_with_web_reader(url: str, timeout: int) -> str:
    """Read a public page through a text reader only after the site blocks direct access."""
    response = requests.get(
        'https://r.jina.ai/' + url,
        headers={
            'Accept': 'text/markdown, text/plain;q=0.9, */*;q=0.8',
            'User-Agent': 'Trade-OS website reader/1.0',
        },
        timeout=max(timeout, 20),
    )
    response.raise_for_status()
    return response.text


# ============ 加载 .env 文件（必须在读取环境变量之前执行）============
def _load_env_file():
    """从项目根目录加载 .env 文件到 os.environ（不覆盖已存在的变量）。

    支持源码运行与 PyInstaller 打包两种场景；解析失败不影响应用启动。
    """
    import sys
    if getattr(sys, 'frozen', False):
        # 打包后：.env 与可执行文件同级
        project_root = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        # 源码运行：engine.py 位于 app/ 下，项目根目录是上一级
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, '.env')
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # 去掉两端成对的引号
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                # 不覆盖已在系统环境里显式设置的变量
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass  # .env 加载失败不应阻止应用启动


_load_env_file()


# ============ 配置（按优先级排列）============

# --- 方案1：LM Studio（推荐，Windows 主机部署）---
# LM Studio 默认地址，Mac 访问时改为 Windows 的 IP
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "")  # 留空则使用 LM Studio 当前加载的模型

# --- 方案2：Ollama ---
OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# --- 方案3：OpenAI（云端备选）---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", OPENAI_BASE_URL)
VISION_API_KEY = os.environ.get("VISION_API_KEY", OPENAI_API_KEY)
VISION_MODEL = os.environ.get("VISION_MODEL", "gpt-4o-mini")

# 选择使用哪个后端：lmstudio / ollama / openai / auto（自动检测）
LLM_BACKEND = os.environ.get("LLM_BACKEND", "auto")


def _call_lm_studio(prompt: str, model: str = None) -> str:
    """
    调用 LM Studio（OpenAI 兼容 API）
    LM Studio 默认运行在 http://localhost:1234/v1
    无需 API Key，或随意填写如 "lm-studio"
    """
    url = f"{LM_STUDIO_URL}/v1/chat/completions"
    headers = {
        "Authorization": "Bearer lm-studio",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or LM_STUDIO_MODEL or "local-model",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[ERROR_LM_STUDIO]"
    except Exception as e:
        return f"[ERROR_LM_STUDIO] {str(e)}"


def _call_ollama(prompt: str, model: str = None) -> str:
    """调用 Ollama 本地模型"""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 2048},
    }
    try:
        resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        return "[ERROR_OLLAMA]"
    except Exception as e:
        return f"[ERROR_OLLAMA] {str(e)}"


def _call_openai(prompt: str, model: str = None) -> str:
    """调用 OpenAI API"""
    if not OPENAI_API_KEY:
        return "[ERROR_OPENAI] 未配置 OPENAI_API_KEY"
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR_OPENAI] {str(e)}"


def extract_text_from_image(image_data_url: str) -> str:
    """Use an OpenAI-compatible vision model to transcribe a CRM conversation screenshot."""
    if not VISION_API_KEY:
        return "[ERROR_VISION] 未配置视觉模型。请设置 VISION_API_KEY、VISION_BASE_URL 和 VISION_MODEL。"
    if not image_data_url.startswith('data:image/') or ';base64,' not in image_data_url:
        return "[ERROR_VISION] 图片格式无效"
    url = f"{VISION_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {VISION_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "请逐行识别这张客户沟通截图中的文字。保留联系人/公司、日期时间、邮箱电话、消息正文和数字规格。只输出识别出的文字，不要分析或补充。"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]}],
        "temperature": 0,
        "max_tokens": 3000,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"[ERROR_VISION] {exc}"


# --- 方案4：DeepSeek（国产，推荐）---
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# --- 方案5：通义千问 Qwen（阿里云百炼）---
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")

# --- 方案6：智谱 GLM ---
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")


def _call_deepseek(prompt: str, model: str = None) -> str:
    """调用 DeepSeek API（OpenAI 兼容）"""
    if not DEEPSEEK_API_KEY:
        return "[ERROR_DEEPSEEK] 未配置 DEEPSEEK_API_KEY"
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 3072,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[ERROR_DEEPSEEK]"
    except Exception as e:
        return f"[ERROR_DEEPSEEK] {str(e)}"


def _call_qwen(prompt: str, model: str = None) -> str:
    """调用通义千问 API（阿里云百炼，OpenAI 兼容）"""
    if not DASHSCOPE_API_KEY:
        return "[ERROR_QWEN] 未配置 DASHSCOPE_API_KEY"
    url = f"{DASHSCOPE_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or DASHSCOPE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 3072,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[ERROR_QWEN]"
    except Exception as e:
        return f"[ERROR_QWEN] {str(e)}"


def _call_glm(prompt: str, model: str = None) -> str:
    """调用智谱 GLM API"""
    if not ZHIPU_API_KEY:
        return "[ERROR_GLM] 未配置 ZHIPU_API_KEY"
    url = f"{ZHIPU_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or ZHIPU_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 3072,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[ERROR_GLM]"
    except Exception as e:
        return f"[ERROR_GLM] {str(e)}"


def ask_llm(prompt: str) -> str:
    """
    统一调用入口

    后端选择逻辑（国产优先）：
    - LLM_BACKEND=deepseek → 只用 DeepSeek
    - LLM_BACKEND=qwen     → 只用通义千问
    - LLM_BACKEND=glm      → 只用智谱 GLM
    - LLM_BACKEND=openai   → 只用 OpenAI
    - LLM_BACKEND=lmstudio → 只用 LM Studio
    - LLM_BACKEND=ollama   → 只用 Ollama
    - LLM_BACKEND=auto     → 自动尝试：DeepSeek → Qwen → GLM → OpenAI → LM Studio → Ollama
    """
    if LLM_BACKEND == "deepseek":
        return _call_deepseek(prompt)
    if LLM_BACKEND == "qwen":
        return _call_qwen(prompt)
    if LLM_BACKEND == "glm":
        return _call_glm(prompt)
    if LLM_BACKEND == "openai":
        return _call_openai(prompt)
    if LLM_BACKEND == "lmstudio":
        result = _call_lm_studio(prompt)
        if not result.startswith("[ERROR"):
            return result
        return result.replace("[ERROR_LM_STUDIO]", "[错误] 无法连接到 LM Studio。请确认 LM Studio 已启动并加载了模型。")
    if LLM_BACKEND == "ollama":
        result = _call_ollama(prompt)
        if not result.startswith("[ERROR"):
            return result
        return result.replace("[ERROR_OLLAMA]", "[错误] 无法连接到 Ollama。请确认 Ollama 已启动。")

    # === auto 模式：自动尝试所有后端（国产优先）===
    # 1. DeepSeek
    result = _call_deepseek(prompt)
    if not result.startswith("[ERROR"):
        return result
    # 2. 通义千问
    result = _call_qwen(prompt)
    if not result.startswith("[ERROR"):
        return result
    # 3. 智谱 GLM
    result = _call_glm(prompt)
    if not result.startswith("[ERROR"):
        return result
    # 4. OpenAI
    result = _call_openai(prompt)
    if not result.startswith("[ERROR"):
        return result
    # 5. LM Studio
    result = _call_lm_studio(prompt)
    if not result.startswith("[ERROR"):
        return result
    # 6. Ollama
    result = _call_ollama(prompt)
    if not result.startswith("[ERROR"):
        return result

    return (
        "[错误] 所有 LLM 后端均不可用。\n\n"
        "请至少配置以下之一：\n"
        "• **DeepSeek**（推荐）：设置环境变量 DEEPSEEK_API_KEY\n"
        "• **通义千问**：设置环境变量 DASHSCOPE_API_KEY\n"
        "• **智谱 GLM**：设置环境变量 ZHIPU_API_KEY\n"
        "• **OpenAI**：设置环境变量 OPENAI_API_KEY\n"
        "• **LM Studio**：在本地启动 LM Studio\n"
        "• **Ollama**：运行 ollama serve"
    )


def quick_chat(question: str, customer_context: str = "") -> str:
    """Evidence-bound CRM question answering; never invent a sales strategy."""
    context_section = ""
    if customer_context:
        context_section = f"\n\n参考的客户背景信息：\n{customer_context}\n"

    prompt = f"""你是 Trade OS 的只读 CRM 助手。你的职责是准确恢复系统记录，不能用通用销售经验填补缺失信息。
{context_section}
用户问题：{question}

只使用提供的 CRM 资料。不要把客户画像、行业常识或过去 AI 分析当成客户当前需求；除非资料明确记录，否则不要推荐产品、报价、交期、物流、样品、竞争策略或联系人。资料不足时直接说明“CRM 中未找到该信息”。"""

    return ask_llm(prompt)


# ==================== 官网内容抓取模块 ====================

class WebsiteContentParser(HTMLParser):
    """自定义 HTML 解析器，提取标题、meta description 和正文文本"""
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.text = []
        self.in_title = False
        self.in_body = False
        self.skip_tags = {'script', 'style', 'noscript', 'footer', 'header', 'nav', 'sidebar'}
        self.current_tag_stack = []

    def handle_starttag(self, tag, attrs):
        self.current_tag_stack.append(tag)
        if tag == 'title':
            self.in_title = True
        elif tag == 'body':
            self.in_body = True
        elif tag == 'meta' and self.in_body:
            attr_dict = dict(attrs)
            if attr_dict.get('name') == 'description' and 'content' in attr_dict:
                self.description = attr_dict['content']

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        elif tag == 'body':
            self.in_body = False
        if self.current_tag_stack and self.current_tag_stack[-1] == tag:
            self.current_tag_stack.pop()

    def handle_data(self, data):
        data = data.strip()
        if not data:
            return

        # Check if we're inside any skipped tag
        for tag in self.current_tag_stack[-3:]:
            if tag in self.skip_tags:
                return

        if self.in_title:
            self.title += data
        elif self.in_body:
            self.text.append(data)

    def get_clean_text(self) -> str:
        """整合提取的所有文本"""
        parts = []
        if self.title:
            parts.append(self.title)
        if self.description:
            parts.append(self.description)
        if self.text:
            parts.append(' '.join(self.text))
        return ' '.join(parts).strip()


def _extract_website_facts(html: str, text: str, page_url: str) -> dict:
    """Extract deterministic, source-bound facts from a public page.

    This deliberately does not infer a person's identity from an email handle.
    It only returns values explicitly present in page metadata, JSON-LD, mailto,
    tel, LinkedIn links, or visible text.  The caller labels these as website
    facts so they can be reviewed before entering the CRM.
    """
    html = html or ''
    text = text or ''
    facts = {
        'name': '', 'description': '', 'emails': [], 'phones': [],
        'linkedin': [], 'contacts': [], 'source': '官网事实',
    }

    def unique(values, limit=8):
        result = []
        for value in values:
            value = str(value or '').strip()
            if value and value.casefold() not in {item.casefold() for item in result}:
                result.append(value)
            if len(result) >= limit:
                break
        return result

    title_match = re.search(r'(?is)<title[^>]*>(.*?)</title>', html)
    title = re.sub(r'<[^>]+>', ' ', title_match.group(1)) if title_match else ''
    title = re.sub(r'\s+', ' ', title).strip()

    meta_values = {}
    for match in re.finditer(
        r'''(?is)<meta\s+[^>]*(?:name|property)\s*=\s*["']([^"']+)["'][^>]*content\s*=\s*["']([^"']*)["'][^>]*>''',
        html,
    ):
        meta_values[match.group(1).casefold()] = re.sub(r'\s+', ' ', match.group(2)).strip()
    site_name = meta_values.get('og:site_name', '')
    description = meta_values.get('description') or meta_values.get('og:description', '')
    facts['description'] = description[:500]
    facts['title'] = title[:300]

    jsonld_values = []
    for match in re.finditer(r"""(?is)<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""", html):
        raw = match.group(1).strip()
        try:
            jsonld_values.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    organizations = []
    people = []
    for root in jsonld_values:
        for item in walk(root):
            item_type = item.get('@type', '')
            types = item_type if isinstance(item_type, list) else [item_type]
            types = {str(item_type).casefold() for item_type in types}
            if types & {'organization', 'corporation', 'localbusiness', 'store'}:
                organizations.append(item)
            if 'person' in types:
                people.append(item)

    organization = next((item for item in organizations if item.get('name')), None)
    facts['name'] = str((organization or {}).get('name') or site_name or '').strip()[:200]
    if not facts['name'] and title:
        title_parts = re.split(r'\s*[|·—–-]\s*', title)
        candidates = [part.strip() for part in title_parts if part.strip()]
        if candidates:
            facts['name'] = (candidates[-1] if len(candidates) > 1 else candidates[0])[:200]

    emails = re.findall(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', html + '\n' + text)
    phones = re.findall(r'(?<!\w)(?:\+?\d[\d .()/-]{6,}\d)(?!\w)', html + '\n' + text)
    linkedin = re.findall(r'(?i)https?://(?:[a-z]{2,3}\.)?linkedin\.com/[^\s"\'<>]+', html)
    facts['emails'] = unique(emails)
    facts['phones'] = unique(phones)
    facts['linkedin'] = unique(linkedin)

    def json_value(item, *keys):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''

    for person in people[:6]:
        contact = {
            'name': json_value(person, 'name'),
            'title': json_value(person, 'jobTitle', 'roleName'),
            'email': json_value(person, 'email'),
            'phone': json_value(person, 'telephone'),
            'linkedin': '',
            'contact_type': 'person',
            'preferred_channel': 'email' if person.get('email') else '',
            'source': '官网事实（结构化数据）',
        }
        same_as = person.get('sameAs') or []
        same_as = same_as if isinstance(same_as, list) else [same_as]
        contact['linkedin'] = next((str(item) for item in same_as if 'linkedin.com/' in str(item).casefold()), '')
        if any(contact.get(key) for key in ('name', 'email', 'phone', 'linkedin')):
            facts['contacts'].append(contact)

    if organization:
        contact_point = organization.get('contactPoint') or []
        contact_point = contact_point if isinstance(contact_point, list) else [contact_point]
        for point in contact_point[:4]:
            if not isinstance(point, dict):
                continue
            contact = {
                'name': '公司公共邮箱',
                'title': json_value(point, 'contactType'),
                'email': json_value(point, 'email'),
                'phone': json_value(point, 'telephone'),
                'contact_type': 'company',
                'preferred_channel': 'email' if point.get('email') else 'phone' if point.get('telephone') else '',
                'source': '官网事实（结构化数据）',
            }
            if any(contact.get(key) for key in ('email', 'phone')):
                facts['contacts'].append(contact)

    return facts


def _browser_tools_content(url: str, timeout: int = 30):
    """Use the installed browser-tools Skill when a CDP Chrome is available."""
    if os.environ.get('CRM_BROWSER_TOOLS_ENABLED', '1').casefold() in {'0', 'false', 'no'}:
        return '', {'attempted': False, 'error': 'browser-tools 已关闭'}
    base_dir = os.environ.get('CRM_BROWSER_TOOLS_DIR', '').strip()
    candidates = [base_dir] if base_dir else []
    candidates.extend([
        os.path.expanduser('~/.codex/skills/browser-tools'),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.codex', 'skills', 'browser-tools'),
    ])
    script = next((os.path.join(path, 'browser-content.js') for path in candidates if path and os.path.isfile(os.path.join(path, 'browser-content.js'))), '')
    if not script:
        return '', {'attempted': False, 'error': '未找到 browser-tools Skill'}
    try:
        completed = subprocess.run(
            ['node', script, url], capture_output=True, text=True,
            timeout=max(5, min(int(timeout), 30)), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return '', {'attempted': True, 'error': str(exc)[:160]}
    if completed.returncode != 0:
        return '', {'attempted': True, 'error': (completed.stderr or completed.stdout or 'browser-tools 读取失败').strip()[-300:]}
    output = (completed.stdout or '').strip()
    final_url = url
    lines = output.splitlines()
    if lines and lines[0].startswith('URL:'):
        final_url = lines.pop(0).split(':', 1)[1].strip() or url
    if lines and lines[0].startswith('Title:'):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    content = '\n'.join(lines).strip()[:5000]
    if len(content) < 40:
        return '', {'attempted': True, 'error': 'browser-tools 未提取到有效正文'}
    return content, {'attempted': True, 'ok': True, 'url': final_url}


def brave_search(query: str, count: int = 5, country: str = 'US', freshness: str = ''):
    """Search Brave when BRAVE_API_KEY is configured; return only public snippets."""
    api_key = os.environ.get('BRAVE_API_KEY', '').strip()
    if not api_key or not query.strip():
        return [], {'enabled': False, 'error': '未配置 BRAVE_API_KEY'}
    try:
        params = {'q': query.strip(), 'count': min(max(int(count), 1), 20), 'country': (country or 'US').upper()}
        if freshness:
            params['freshness'] = freshness
        response = requests.get(
            'https://api.search.brave.com/res/v1/web/search',
            params=params,
            headers={
                'Accept': 'application/json',
                'Accept-Encoding': 'gzip',
                'X-Subscription-Token': api_key,
                'User-Agent': 'Trade-OS website enrichment/1.0',
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        results = []
        for item in ((payload.get('web') or {}).get('results') or [])[:params['count']]:
            link = str(item.get('url') or '').strip()
            if not link:
                continue
            results.append({
                'title': str(item.get('title') or '').strip()[:240],
                'url': link[:1000],
                'snippet': re.sub(r'\s+', ' ', str(item.get('description') or '')).strip()[:800],
                'age': str(item.get('age') or item.get('page_age') or '').strip()[:80],
            })
        return results, {'enabled': True, 'ok': True}
    except Exception as exc:
        return [], {'enabled': True, 'ok': False, 'error': str(exc)[:240]}


def fetch_website_content(url: str, timeout: int = 15, return_meta: bool = False, deep: bool = False):
    """
    抓取网页并提取纯文本内容
    - 自动添加 https:// 前缀（如果缺少协议）
    - 使用 HTMLParser 提取标题、meta description 和正文
    - 返回限制在 5000 字符以内
    - 出错返回空字符串，不抛出异常
    """
    # 自动补全协议
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url.lstrip('/')

    meta = {
        'ok': False, 'http_status': None, 'error_code': '', 'error_message': '',
        'pages_read': [], 'read_method': 'direct', 'website_facts': {},
        'browser_tools_attempted': False,
    }

    def finish(content):
        return (content, meta) if return_meta else content

    def read_page(target_url):
        req = urllib.request.Request(
            target_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Upgrade-Insecure-Requests': '1',
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_website_ssl_context()) as response:
                html = response.read().decode('utf-8', errors='replace')
                status = getattr(response, 'status', None)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429):
                raise
            try:
                html = _read_with_web_reader(target_url, timeout)
                status = 200
                meta['read_method'] = 'web_reader_fallback'
            except requests.RequestException:
                raise exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, 'reason', '') or exc).lower()
            if 'certificate_verify_failed' not in reason:
                raise
            # Windows curl uses Schannel, the same system trust mechanism used
            # by Edge and Chrome. It remains certificate-verified and can build
            # chains that Python's OpenSSL cannot complete.
            html = _read_with_windows_curl(target_url, timeout)
            status = None
        if meta['read_method'] == 'web_reader_fallback':
            return html.strip(), '', status
        parser = WebsiteContentParser()
        parser.feed(html)
        return parser.get_clean_text(), html, status

    def browser_fallback():
        browser_text, browser_meta = _browser_tools_content(url, timeout=max(timeout, 20))
        meta['browser_tools_attempted'] = bool(browser_meta.get('attempted'))
        if browser_text:
            final_url = browser_meta.get('url') or url
            meta.update(ok=True, read_method='browser-tools', pages_read=[final_url], website_facts=_extract_website_facts('', browser_text, final_url))
            return browser_text
        return ''

    try:
        try:
            clean_text, html_content, status = read_page(url)
        except urllib.error.URLError as original_error:
            # 有些小型官网只给 www 子域名配置了有效证书。识别时安全地
            # 尝试同域名的 www 版本与 HTTP 版本，避免首页因证书配置问题直接失败。
            parsed_url = urlparse(url)
            host = parsed_url.netloc
            fallback_urls = []
            if host and not host.lower().startswith('www.'):
                fallback_urls.append(parsed_url._replace(netloc='www.' + host).geturl())
            if parsed_url.scheme == 'https':
                fallback_urls.append(parsed_url._replace(scheme='http').geturl())
            fallback_success = None
            for fallback_url in fallback_urls:
                try:
                    fallback_success = (*read_page(fallback_url), fallback_url)
                    break
                except Exception:
                    continue
            if not fallback_success:
                raise original_error
            clean_text, html_content, status, url = fallback_success
        meta['http_status'] = status
        meta['pages_read'].append(url)
        if not clean_text:
            browser_text = browser_fallback()
            if browser_text:
                return finish(browser_text)
            meta.update(error_code='empty_content', error_message='网站可以打开，但没有读取到有效正文')
            return finish('')

        # 公司介绍往往不在首页。智能识别时只读取同域名下最多两个 About / Company 类页面。
        extra_texts = []
        extra_html = []
        if deep:
            base_host = urlparse(url).netloc.lower().replace('www.', '')
            hrefs = re.findall(r'''(?is)<a[^>]+href\s*=\s*["']([^"'#?]+)["']''', html_content)
            about_words = ('about', 'about-us', 'aboutus', 'company', 'who-we-are', 'our-story', 'profile', 'corporate', '关于', '公司')
            candidates = []
            for href in hrefs:
                target = urljoin(url, href.strip())
                parsed = urlparse(target)
                host = parsed.netloc.lower().replace('www.', '')
                path = (parsed.path or '').lower()
                if host != base_host or not any(word in path for word in about_words):
                    continue
                if target not in candidates:
                    candidates.append(target)
            for target in candidates[:2]:
                try:
                    page_text, page_html, _ = read_page(target)
                    if page_text:
                        extra_texts.append(page_text[:2200])
                        if page_html:
                            extra_html.append(page_html)
                        meta['pages_read'].append(target)
                except Exception:
                    continue

        combined_text = '\n\n'.join([clean_text[:2600]] + extra_texts)[:5000]
        meta['website_facts'] = _extract_website_facts(html_content + ''.join(extra_html), combined_text, url)
        if deep and len(combined_text.strip()) < 180:
            browser_text = browser_fallback()
            if browser_text:
                return finish(browser_text)
        meta['ok'] = True
        return finish(combined_text)

    except urllib.error.HTTPError as exc:
        browser_text = browser_fallback()
        if browser_text:
            return finish(browser_text)
        meta.update(http_status=exc.code, error_code='http_error', error_message=f'网站返回 HTTP {exc.code}')
        return finish('')
    except urllib.error.URLError as exc:
        browser_text = browser_fallback()
        if browser_text:
            return finish(browser_text)
        reason = str(getattr(exc, 'reason', '') or exc)
        if 'timed out' in reason.lower():
            meta.update(error_code='timeout', error_message='连接网站超时')
        else:
            meta.update(error_code='unreachable', error_message=f'网站无法连接：{reason[:120]}')
        return finish('')
    except TimeoutError:
        browser_text = browser_fallback()
        if browser_text:
            return finish(browser_text)
        meta.update(error_code='timeout', error_message='连接网站超时')
        return finish('')
    except Exception as exc:
        browser_text = browser_fallback()
        if browser_text:
            return finish(browser_text)
        meta.update(error_code='unknown', error_message=f'读取网站失败：{str(exc)[:120]}')
        return finish('')


def compute_text_similarity(text1: str, text2: str) -> float:
    """
    使用 SequenceMatcher 计算两段文本的相似度
    返回 0.0 ~ 1.0 之间的浮点数，越大越相似
    """
    if not text1 or not text2:
        return 0.0
    matcher = difflib.SequenceMatcher(None, text1, text2)
    return matcher.ratio()


def hash_content(text: str) -> str:
    """对文本内容计算 MD5 哈希，返回 32 位十六进制字符串"""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def check_website_changes_by_level(db_path: str, levels: list, max_changes: int = 10) -> dict:
    """
    按客户等级分层检查官网变化
    
    参数:
        db_path: SQLite 数据库路径
        levels: 要检查的客户等级列表，如 ['A', 'B'] 或 ['C+']
        max_changes: 最大变化提醒数
    
    返回: {'checked': N, 'changed': N, 'errors': N, 'reminders_created': N}
    """
    import sqlite3
    from datetime import datetime, timedelta
    
    result = {'checked': 0, 'changed': 0, 'errors': 0, 'reminders_created': 0}
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 查询目标客户
    placeholders = ','.join(['?' for _ in levels])
    c.execute(f'''SELECT * FROM customers 
                  WHERE website IS NOT NULL AND website != '' 
                  AND level IN ({placeholders})
                  AND (is_deleted = 0 OR is_deleted IS NULL)
                  ORDER BY 
                    CASE level 
                        WHEN 'A' THEN 1 
                        WHEN 'B' THEN 2 
                        WHEN 'C+' THEN 3 
                        WHEN 'C' THEN 4 
                        ELSE 5 
                    END''', levels)
    customers = c.fetchall()
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    for customer in customers:
        if result['reminders_created'] >= max_changes:
            break
        
        customer_id = customer['id']
        website = customer['website']
        company = customer.get('company', '') or customer.get('name', '')
        
        # 抓取当前官网内容
        current_text = fetch_website_content(website)
        current_hash = hash_content(current_text) if current_text else ''
        
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if not current_text:
            # 网站不可达
            c.execute('''INSERT INTO web_monitor_logs 
                         (customer_id, url, status, content_hash, checked_at)
                         VALUES (?, ?, 'error', ?, ?)''',
                      (customer_id, website, current_hash, now_str))
            result['errors'] += 1
            result['checked'] += 1
            continue
        
        # 查找上次记录
        c.execute('''SELECT * FROM web_monitor_logs 
                     WHERE customer_id = ? AND status = 'ok'
                     ORDER BY checked_at DESC LIMIT 1''', (customer_id,))
        last_log = c.fetchone()
        
        if last_log and last_log['content_hash']:
            last_hash = last_log['content_hash']
            
            if current_hash != last_hash:
                # 内容有变化，计算相似度
                last_text = last_log.get('content_snippet', '')
                if last_text:
                    similarity = compute_text_similarity(last_text, current_text)
                else:
                    similarity = 0.0
                
                if similarity < 0.8:
                    # 显著变化，生成 LLM 摘要
                    change_summary = ask_llm(
                        f"以下是一家客户（{company}）官网的内容变化，请用一句话总结变化了什么：\n\n"
                        f"旧内容：{last_text[:500]}\n\n新内容：{current_text[:500]}"
                    )
                    if change_summary.startswith("[错误"):
                        change_summary = f"官网内容有显著变化（相似度 {similarity:.0%}）"
                    
                    # 创建提醒
                    c.execute('''INSERT INTO reminders 
                                 (customer_id, reminder_type, remind_date, content, is_done, created_at)
                                 VALUES (?, 'web_change', ?, ?, 0, ?)''',
                              (customer_id, today, 
                               f"官网变化: {company} - {change_summary[:200]}",
                               now_str))
                    reminder_id = c.lastrowid
                    
                    c.execute('''INSERT INTO web_monitor_logs 
                                 (customer_id, url, status, content_hash, content_snippet, 
                                  change_summary, reminder_id, checked_at)
                                 VALUES (?, ?, 'changed', ?, ?, ?, ?, ?)''',
                              (customer_id, website, current_hash, current_text[:500],
                               change_summary[:500], reminder_id, now_str))
                    result['changed'] += 1
                    result['reminders_created'] += 1
                else:
                    # 微小变化，仅更新 hash
                    c.execute('''INSERT INTO web_monitor_logs 
                                 (customer_id, url, status, content_hash, content_snippet, checked_at)
                                 VALUES (?, ?, 'ok', ?, ?, ?)''',
                              (customer_id, website, current_hash, current_text[:500], now_str))
            else:
                # 无变化
                c.execute('''INSERT INTO web_monitor_logs 
                             (customer_id, url, status, content_hash, content_snippet, checked_at)
                             VALUES (?, ?, 'ok', ?, ?, ?)''',
                          (customer_id, website, current_hash, current_text[:500], now_str))
        else:
            # 首次检查，记录初始状态
            c.execute('''INSERT INTO web_monitor_logs 
                         (customer_id, url, status, content_hash, content_snippet, checked_at)
                         VALUES (?, ?, 'ok', ?, ?, ?)''',
                      (customer_id, website, current_hash, current_text[:500], now_str))
        
        result['checked'] += 1
    
    conn.commit()
    conn.close()
    return result


