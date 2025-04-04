import os
import re
import base64
import json
import requests
import feedparser
import urllib.parse
import concurrent.futures
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import time
import traceback
import logging
from tqdm import tqdm
import tiktoken  # OpenAI 토큰 계산용

# Gmail API 관련 모듈
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import google_auth_oauthlib.flow  # OAuth 인증

# 전역 변수: 메모리에만 Gmail 자격증명 저장 (token.json 백업 없이)
GMAIL_CREDS = None


# OpenAI API 사용량 추적 (tiktoken 사용)
class OpenAIUsageTracker:
    def __init__(self, exchange_rate=1480):
        self.exchange_rate = exchange_rate
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_requests = 0
        try:
            self.encoder = tiktoken.encoding_for_model("gpt-4o-mini")
        except Exception:
            self.encoder = tiktoken.get_encoding("cl100k_base")
            logging.warning("gpt-4o-mini 인코더 로드 실패, cl100k_base 사용")

    def count_tokens(self, text):
        return len(self.encoder.encode(text)) if text else 0

    def track_request(self, messages):
        tokens = sum(self.count_tokens(msg.get("content", "")) for msg in messages)
        self.total_input_tokens += tokens
        self.total_requests += 1
        return tokens

    def track_response(self, response_text):
        tokens = self.count_tokens(response_text)
        self.total_output_tokens += tokens
        return tokens

    def calculate_cost(self):
        input_cost_usd = (self.total_input_tokens / 1_000_000) * 0.15
        output_cost_usd = (self.total_output_tokens / 1_000_000) * 0.60
        total_cost_usd = input_cost_usd + output_cost_usd
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_requests": self.total_requests,
            "input_cost_usd": input_cost_usd,
            "output_cost_usd": output_cost_usd,
            "total_cost_usd": total_cost_usd,
            "input_cost_krw": input_cost_usd * self.exchange_rate,
            "output_cost_krw": output_cost_usd * self.exchange_rate,
            "total_cost_krw": total_cost_usd * self.exchange_rate,
        }

    def log_usage(self):
        data = self.calculate_cost()
        logging.info("===== OpenAI API 사용량 및 비용 =====")
        logging.info(f"총 요청 수: {data['total_requests']:,}회")
        logging.info(
            f"입력 토큰: {data['input_tokens']:,}개 (${data['input_cost_usd']:.6f}, ₩{data['input_cost_krw']:.2f})"
        )
        logging.info(
            f"출력 토큰: {data['output_tokens']:,}개 (${data['output_cost_usd']:.6f}, ₩{data['output_cost_krw']:.2f})"
        )
        logging.info(
            f"전체 토큰: {data['total_tokens']:,}개 (${data['total_cost_usd']:.6f}, ₩{data['total_cost_krw']:.2f})"
        )
        logging.info("====================================")
        return data


# 커스텀 로깅 포매터
class ColoredFormatter(logging.Formatter):
    RESET = "\x1b[0m"
    COLORS = {
        logging.DEBUG: "\x1b[37;20m",
        logging.INFO: "\x1b[32;20m",
        logging.WARNING: "\x1b[33;20m",
        logging.ERROR: "\x1b[31;20m",
        logging.CRITICAL: "\x1b[31;1m",
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        fmt = logging.Formatter(
            f"{color}%(asctime)s - %(levelname)s - %(message)s{self.RESET}",
            "%Y-%m-%d %H:%M:%S",
        )
        return fmt.format(record)


# 로그 파일 생성 (ynanews_YYYYMMDD.log)
log_date = datetime.now().strftime("%Y%m%d")
log_filename = f"ynanews_{log_date}.log"
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, log_filename)
file_handler = logging.FileHandler(log_path, encoding="utf-8")
file_handler.setFormatter(ColoredFormatter())
file_handler.setLevel(logging.ERROR)
logger = logging.getLogger()
logger.handlers = [file_handler]
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)
logging.info(f"로그 파일: {log_path} (ERROR 레벨만 기록)")
logging.info("콘솔 출력: INFO 이상")


def find_credentials_file():
    for path in [
        "credentials.json",
        "client_secret.json",
        os.path.join(os.path.expanduser("~"), "credentials.json"),
        os.path.join(os.path.expanduser("~"), "client_secret.json"),
    ]:
        if os.path.exists(path):
            logging.info(f"인증 파일 발견: {path}")
            return path
    logging.error("인증 파일을 찾을 수 없습니다.")
    return None


def show_credentials_help():
    print("\n" + "=" * 80)
    print("Gmail API 인증 설정 방법")
    print("=" * 80)
    print("1. Google Cloud Console 접속")
    print("2. 프로젝트 생성 또는 선택")
    print("3. API 및 서비스 > 사용자 인증 정보")
    print("4. OAuth 클라이언트 ID 생성 (데스크톱 앱)")
    print("5. 다운로드 받은 'credentials.json'을 스크립트 폴더에 저장")
    print("6. Gmail API 활성화")
    print("=" * 80 + "\n")


def authenticate_with_oauth():
    path = find_credentials_file()
    if not path:
        show_credentials_help()
        return False
    try:
        flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
            path,
            ["https://www.googleapis.com/auth/gmail.send"],
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        print("\n" + "=" * 50)
        print("Gmail API 인증 필요")
        print("다음 URL에서 인증 후 코드를 입력하세요:")
        print(auth_url)
        print("=" * 50 + "\n")
        code = input("인증 코드: ").strip()
        if not code:
            logging.error("인증 코드 미입력")
            return False
        flow.fetch_token(code=code)
        global GMAIL_CREDS
        GMAIL_CREDS = flow.credentials  # 메모리 내 저장
        logging.info("Gmail 인증 성공")
        return True
    except Exception as e:
        logging.error(f"OAuth 인증 오류: {e}")
        traceback.print_exc()
        return False


def check_gmail_credentials():
    token_path = "token.json"
    if not os.path.exists(token_path):
        logging.info("토큰 파일 없음, 인증 진행")
        return authenticate_with_oauth()
    try:
        creds = Credentials.from_authorized_user_info(
            json.loads(open(token_path, "r").read())
        )
    except Exception as e:
        logging.error(f"토큰 파일 로드 오류: {e}")
        return authenticate_with_oauth()
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.info("토큰 갱신 시도")
            try:
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                logging.info("토큰 갱신 완료")
                return True
            except Exception as e:
                logging.error(f"토큰 갱신 오류: {e}")
                return authenticate_with_oauth()
        else:
            logging.error("유효한 토큰 없음, 재인증 필요")
            return authenticate_with_oauth()
    global GMAIL_CREDS
    GMAIL_CREDS = creds
    return True


def send_email_with_attachment(subject, body, attachment_path=None, html_content=None):
    try:
        if not check_gmail_credentials():
            logging.error("Gmail 인증 실패")
            return False
        service = build("gmail", "v1", credentials=GMAIL_CREDS)
        sender = os.getenv("GMAIL_SENDER_EMAIL")
        recipients = [
            e.strip() for e in os.getenv("GMAIL_RECIPIENTS", "").split(",") if e.strip()
        ]
        if not sender or not recipients:
            logging.error("Gmail 환경변수 누락")
            return False
        msg = MIMEMultipart("alternative" if html_content else "mixed")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))
        if html_content:
            msg.attach(MIMEText(html_content, "html"))
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                attach = MIMEApplication(
                    f.read(), Name=os.path.basename(attachment_path)
                )
            attach["Content-Disposition"] = (
                f'attachment; filename="{os.path.basename(attachment_path)}"'
            )
            msg.attach(attach)
            logging.info(f"첨부파일: {os.path.basename(attachment_path)}")
        raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_msg})
            .execute()
        )
        logging.info(f"메일 발송 완료: 메시지 ID {sent['id']}")
        return True
    except Exception as e:
        logging.error(f"메일 발송 오류: {e}")
        traceback.print_exc()
        return False


def main():
    logging.info("프로그램 시작")
    load_dotenv()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    # .env 파일의 SEARCH_KEYWORDS 변수 사용 (빈 값은 제외)
    keywords = [
        kw.strip() for kw in os.getenv("SEARCH_KEYWORDS", "").split(",") if kw.strip()
    ]
    usage_tracker = OpenAIUsageTracker(exchange_rate=1480)
    logging.info(f"OpenAI API Key Loaded: {bool(OPENAI_API_KEY)}")
    logging.info(f"검색 키워드: {keywords}")

    # 1. YNA RSS 피드 기사 수집
    rss_url = "https://www.yna.co.kr/rss/news.xml"
    articles = []
    for attempt in range(3):
        try:
            logging.info(f"RSS 접속 시도 {attempt+1}/3")
            resp = requests.get(rss_url, timeout=10)
            resp.raise_for_status()
            if resp.text:
                feed = feedparser.parse(resp.text)
                articles = feed.get("entries", [])[:200]
                if not articles:
                    logging.warning("RSS 기사 없음")
                    time.sleep(2)
                    continue
                else:
                    logging.info("RSS 기사 수집 성공")
                    break
        except Exception as e:
            logging.error(f"RSS 접속 오류: {e}")
            time.sleep(2)
    logging.info(f"전체 기사 수: {len(articles)}")
    if articles:
        logging.info(f"첫 기사 제목: {articles[0].title}")

    # 2. 최근 2일 기사 필터링
    recent = []
    two_days = datetime.now() - timedelta(days=2)
    for art in articles:
        try:
            pub = datetime(*art.published_parsed[:6])
            if pub >= two_days:
                art.pub_date_obj = pub
                recent.append(art)
        except Exception:
            continue
    logging.info(f"최근 2일 기사 수: {len(recent)}")

    # 2.5. 본문 추출 함수
    def get_full_article_content(url):
        try:
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.select(
                "script, style, nav, footer, .ad, .advertisement, .banner, .copyright"
            ):
                tag.decompose()
            selectors = [
                "div.story-news",
                "div.article-body",
                "article",
                "div.article",
                "div.news-content",
                "div.content",
                "div.entry-content",
                "div.article-content",
            ]
            for sel in selectors:
                container = soup.select_one(sel)
                if container:
                    ps = container.find_all("p")
                    if ps:
                        text = "\n".join(p.get_text(strip=True) for p in ps)
                        if len(text) > 200:
                            return text
                    text = container.get_text(separator="\n").strip()
                    if len(text) > 200:
                        return text
            meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta and meta.get("content"):
                cont = meta["content"].strip()
                if len(cont) > 100:
                    return cont
            return soup.get_text(separator="\n").strip()
        except Exception as e:
            logging.error(f"본문 추출 오류: {url} - {e}")
            return ""

    # 3. 기사 요약 (OpenAI API)
    def summarize_text(text):
        short_text = text if len(text) < 1000 else text[:1000]
        prompt = f"다음 뉴스 기사 본문을 3문장 이내로 간략히 요약해줘:\n\n{short_text}"
        messages = [
            {
                "role": "system",
                "content": "You are an assistant that summarizes news articles in Korean.",
            },
            {"role": "user", "content": prompt},
        ]
        usage_tracker.track_request(messages)
        data = {"model": "gpt-4o-mini", "messages": messages, "temperature": 0.3}
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions", json=data, headers=headers
            )
            if resp.status_code == 200:
                summary = resp.json()["choices"][0]["message"]["content"].strip()
                usage_tracker.track_response(summary)
                return summary
            else:
                logging.error(f"요약 API 오류: {resp.status_code} - {resp.text}")
                return ""
        except Exception as e:
            logging.error(f"요약 요청 오류: {e}")
            return ""

    # 4. 키워드 매칭: 본문에 특정 키워드가 2회 이상 등장
    def is_relevant_body(text):
        if not text:
            return False
        lower = text.lower()
        for kw in keywords:
            if lower.count(kw.lower()) >= 2:
                return True
        return False

    # 5. 기사 필터링
    filtered = []

    def fetch_and_filter(art, pbar):
        content = get_full_article_content(art.link)
        pbar.update(1)
        if is_relevant_body(content):
            pbar.write(f"매칭 기사: {art.title[:40]}...")
            return {
                "title": art.title,
                "content": content,
                "url": art.link,
                "pubdate": art.pub_date_obj,
            }
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = []
        with tqdm(
            total=len(recent),
            desc="Fetching & Filtering",
            dynamic_ncols=True,
            colour="green",
        ) as pbar:
            for art in recent:
                futures.append(executor.submit(fetch_and_filter, art, pbar))
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    filtered.append(res)
    logging.info(f"필터링 후 기사 수: {len(filtered)}")

    # 6. 기사 요약
    with tqdm(
        total=len(filtered), desc="Summarizing", dynamic_ncols=True, colour="blue"
    ) as pbar:
        for art in filtered:
            art["article_summary"] = summarize_text(art["content"])
            pbar.write(f"요약 완료: {art['title'][:40]}...")
            pbar.update(1)

    # 7. 기사 제목 유사도 (OpenAI API)
    def calculate_similarity(t1, t2):
        prompt = (
            f"두 기사 제목의 유사도를 0~100 정수로만 답해:\n제목1: {t1}\n제목2: {t2}"
        )
        messages = [
            {
                "role": "system",
                "content": "You are an assistant that calculates similarity percentage.",
            },
            {"role": "user", "content": prompt},
        ]
        usage_tracker.track_request(messages)
        data = {"model": "gpt-4o-mini", "messages": messages, "temperature": 0.0}
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        for _ in range(3):
            try:
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=data,
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    ans = resp.json()["choices"][0]["message"]["content"].strip()
                    usage_tracker.track_response(ans)
                    m = re.search(r"(\d+(\.\d+)?)", ans)
                    if m:
                        return float(m.group(1))
                    return 0.0
                else:
                    logging.error(f"유사도 API 오류: {resp.status_code} - {resp.text}")
            except Exception as e:
                logging.error(f"유사도 요청 오류: {e}")
        return 0.0

    # 8. 구글 뉴스 중복 기사 검색 (유사도 70% 이상 최대 3건)
    def get_google_duplicates(art, pbar=None):
        query = urllib.parse.quote(art["title"])
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        dups = []
        try:
            feed = feedparser.parse(url)
            with tqdm(
                total=min(len(feed["entries"]), 10),
                desc=f"Searching: {art['title'][:20]}...",
                dynamic_ncols=True,
                colour="magenta",
                leave=False,
            ) as ibar:
                for entry in feed["entries"][:10]:
                    sim = calculate_similarity(art["title"], entry.title)
                    ibar.update(1)
                    if sim >= 70:
                        dups.append(
                            {"title": entry.title, "url": entry.link, "similarity": sim}
                        )
                        if len(dups) >= 3:
                            break
        except Exception as e:
            logging.error(f"구글 중복 검색 오류: {e}")
        if pbar:
            pbar.write(f"중복 검색 완료: {art['title'][:40]}...")
            pbar.update(1)
        return dups

    with tqdm(
        total=len(filtered),
        desc="Finding Duplicates",
        dynamic_ncols=True,
        colour="yellow",
    ) as pbar:
        for art in filtered:
            art["duplicates"] = get_google_duplicates(art, pbar)

    usage_data = usage_tracker.log_usage()
    today_str = datetime.now().strftime("%Y-%m-%d")
    email_subject = f"YNA 뉴스 결과 - {today_str}"

    article_count = len(filtered)
    count_str = "1건" if article_count == 1 else f"{article_count}건"

    # 이메일 본문 (텍스트)
    if article_count > 0:
        email_body = f"오늘 날짜: {today_str}\n검색 키워드: {', '.join(keywords)}\n총 뉴스 기사 수: {count_str}\n자세한 내용은 HTML 버전을 확인하세요."
    else:
        email_body = f"오늘 날짜: {today_str}\n검색 키워드: {', '.join(keywords)}\n오늘은 관련 기사가 없습니다."

    # HTML 이메일 본문 (표 디자인 개선)
    html_head = f"""
    <html>
    <head>
      <meta charset="UTF-8">
      <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
        h2 {{ margin-bottom: 1rem; }}
        .summary-list {{ margin-left: 1rem; list-style: disc; }}
        .article-table {{
          width: 100%; border-collapse: collapse; margin-bottom: 30px;
        }}
        .article-table th, .article-table td {{
          border: 1px solid #ddd; padding: 8px; vertical-align: top;
        }}
        .article-table th {{ background-color: #f2f2f2; font-weight: bold; }}
        .duplicates {{ margin-top: 0.5rem; color: #555; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
      </style>
    </head>
    <body>
      <div class="container">
        <h2>YNA 뉴스 결과 - {today_str}</h2>
        <p>검색 키워드: <strong>{', '.join(keywords)}</strong></p>
        <p>총 뉴스 기사 수: <strong>{count_str}</strong></p>
        <h3>OpenAI API 사용량</h3>
        <ul class="summary-list">
          <li>입력 토큰: {usage_data['input_tokens']:,}개 ( ${usage_data['input_cost_usd']:.6f}, ₩{usage_data['input_cost_krw']:.2f} )</li>
          <li>출력 토큰: {usage_data['output_tokens']:,}개 ( ${usage_data['output_cost_usd']:.6f}, ₩{usage_data['output_cost_krw']:.2f} )</li>
          <li>전체 토큰: {usage_data['total_tokens']:,}개 ( ${usage_data['total_cost_usd']:.6f}, ₩{usage_data['total_cost_krw']:.2f} )</li>
        </ul>
    """

    html_body_articles = ""
    if article_count > 0:
        for idx, art in enumerate(filtered, start=1):
            dup_html = ""
            if art.get("duplicates"):
                for d in art["duplicates"]:
                    dup_html += f'<div>• <a href="{d["url"]}" target="_blank">{d["title"]}</a> ({d["similarity"]:.1f}%)</div>'
            else:
                dup_html = "<div>없음</div>"
            html_body_articles += f"""
            <table class="article-table">
              <tr><th colspan="2">기사 {idx}</th></tr>
              <tr>
                <th style="width:120px;">제목</th>
                <td><a href="{art["url"]}" target="_blank">{art["title"]}</a></td>
              </tr>
              <tr>
                <th>요약</th>
                <td>{art.get("article_summary", "요약 없음")}</td>
              </tr>
              <tr>
                <th>발행일자</th>
                <td>{art["pubdate"].strftime('%Y-%m-%d %H:%M')}</td>
              </tr>
              <tr>
                <th>유사뉴스</th>
                <td class="duplicates">{dup_html}</td>
              </tr>
            </table>
            """
    else:
        html_body_articles = "<p>관련 기사가 없습니다.</p>"

    html_tail = """
      </div>
    </body>
    </html>
    """
    full_html = html_head + html_body_articles + html_tail

    # 이메일 발송: 보낼 기사가 없으면 발송하지 않음
    if os.getenv("SEND_EMAIL", "").lower() == "true":
        if article_count > 0:
            logging.info("이메일 발송 시도 중...")
            try:
                sender = os.getenv("GMAIL_SENDER_EMAIL")
                recipients = [
                    e.strip()
                    for e in os.getenv("GMAIL_RECIPIENTS", "").split(",")
                    if e.strip()
                ]
                if not (sender and recipients):
                    logging.error("GMAIL_SENDER_EMAIL 또는 GMAIL_RECIPIENTS 누락")
                    print("이메일 환경변수 확인 요망.")
                elif not check_gmail_credentials():
                    logging.error("Gmail 인증 실패")
                    print("Gmail 인증 필요.")
                else:
                    if send_email_with_attachment(
                        email_subject, email_body, None, full_html
                    ):
                        logging.info(f"이메일 발송 성공: {email_subject}")
                        print(f"이메일 발송 완료 - 수신자: {', '.join(recipients)}")
            except Exception as e:
                logging.error(f"메일 발송 오류: {e}")
                traceback.print_exc()
                print("메일 발송 오류 발생, 로그 확인 요망.")
        else:
            logging.info("보낼 기사가 없으므로 이메일 발송을 건너뜁니다.")
    else:
        logging.info("이메일 발송 비활성화 (SEND_EMAIL=false)")

    logging.info("프로그램 종료")


if __name__ == "__main__":
    main()
