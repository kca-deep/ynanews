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
from tabulate import tabulate
import openpyxl
from openpyxl.styles import Alignment, Font
from bs4 import BeautifulSoup
import time
import traceback
import logging
from tqdm import tqdm

# Gmail API 관련 모듈
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ANSI 색상 코드 기반의 커스텀 Formatter 정의
class ColoredFormatter(logging.Formatter):
    RESET = "\x1b[0m"
    COLORS = {
        logging.DEBUG: "\x1b[37;20m",  # 회색
        logging.INFO: "\x1b[32;20m",  # 초록색
        logging.WARNING: "\x1b[33;20m",  # 노란색
        logging.ERROR: "\x1b[31;20m",  # 빨간색
        logging.CRITICAL: "\x1b[31;1m",  # 진한 빨간색
    }

    def format(self, record):
        log_color = self.COLORS.get(record.levelno, self.RESET)
        formatter = logging.Formatter(
            f"{log_color}%(asctime)s - %(levelname)s - %(message)s{self.RESET}",
            "%Y-%m-%d %H:%M:%S",
        )
        return formatter.format(record)


# 로그 파일명을 타임스탬프를 포함해서 생성
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"ynanews-{timestamp}.log"

# FileHandler를 이용해서 로그를 파일에 기록하도록 설정 (인코딩 'utf-8' 지정)
file_handler = logging.FileHandler(log_filename, encoding="utf-8")
file_handler.setFormatter(ColoredFormatter())

# 콘솔 핸들러는 제거하고 파일 핸들러만 사용
logger = logging.getLogger()
logger.handlers = [file_handler]
logger.setLevel(logging.INFO)


def main():
    logging.info("프로그램 시작")
    # 0. .env 파일에서 환경 변수 로드
    load_dotenv()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    keywords_env = os.getenv("SEARCH_KEYWORDS", "")
    keyword_list = (
        [kw.strip() for kw in keywords_env.split(",")]
        if keywords_env
        else ["ICT", "AI"]
    )

    logging.info("OpenAI API Key Loaded: %s", bool(OPENAI_API_KEY))
    logging.info("검색 키워드: %s", keyword_list)

    # 1. YNA RSS 피드에서 기사 수집
    yna_rss_url = "https://www.yna.co.kr/rss/news.xml"
    rss_data = feedparser.parse(yna_rss_url)
    articles = rss_data["entries"]
    logging.info("전체 기사 개수: %d", len(articles))
    if articles:
        logging.info("첫 번째 기사 제목: %s", articles[0].title)

    # 2. 최근 2일 이내 기사 필터링
    two_days_ago = datetime.now() - timedelta(days=2)
    recent_articles = []
    for art in articles:
        try:
            pub_date = datetime(*art.published_parsed[:6])
        except Exception:
            continue
        if pub_date >= two_days_ago:
            art.pub_date_obj = pub_date
            recent_articles.append(art)
    logging.info("최근 2일 이내 기사 개수: %d", len(recent_articles))

    # 2.5. 실제 URL에서 기사 본문을 추출하는 함수
    def get_full_article_content(url):
        logging.info("Fetching article content from: %s", url)
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # 1) 'div.story-news' 컨테이너 내의 <p> 태그들만 추출
            container = soup.select_one("div.story-news")
            if container:
                paragraphs = container.find_all("p")
                if paragraphs:
                    content = "\n".join(p.get_text(strip=True) for p in paragraphs)
                    if len(content) > 200:
                        logging.debug(
                            "Extracted article content from paragraphs in 'div.story-news'"
                        )
                        return content
                text = container.get_text(separator="\n").strip()
                if len(text) > 200:
                    return text

            # 2) 'div.article-body' 또는 'article' 선택자 사용
            for sel in ["div.article-body", "article"]:
                container = soup.select_one(sel)
                if container:
                    paragraphs = container.find_all("p")
                    if paragraphs:
                        content = "\n".join(p.get_text(strip=True) for p in paragraphs)
                        if len(content) > 200:
                            logging.debug(
                                "Extracted article content from paragraphs in '%s'", sel
                            )
                            return content
                    text = container.get_text(separator="\n").strip()
                    if len(text) > 200:
                        return text

            # 3) fallback: 전체 페이지 텍스트 반환
            logging.debug("No specific container found, returning full page text")
            return soup.get_text(separator="\n").strip()
        except Exception as e:
            logging.error(
                "Error fetching article content from URL: %s - %s", url, str(e)
            )
            traceback.print_exc()
            return ""

    # 3. OpenAI API를 이용해 기사 본문 요약 생성 함수
    def summarize_text(text):
        truncated_text = text if len(text) < 1000 else text[:1000]
        prompt = f"다음 뉴스 기사 본문을 간략하게 요약해줘. 요약은 3문장 이내로 작성해줘:\n\n{truncated_text}"
        data = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an assistant that summarizes news articles in Korean.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        req_headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            "https://api.openai.com/v1/chat/completions", json=data, headers=req_headers
        )
        if response.status_code != 200:
            logging.error(
                "OpenAI API error in summarization: %d - %s",
                response.status_code,
                response.text,
            )
            return ""
        result = response.json()
        summary = result["choices"][0]["message"]["content"].strip()
        return summary

    # 4. 키워드 검사 함수
    def is_relevant(art_text):
        return any(keyword in art_text for keyword in keyword_list)

    # 5. 초기 필터링: RSS의 title과 summary로 기사 선별
    pre_filtered_articles = [
        art
        for art in recent_articles
        if is_relevant(art.title) or is_relevant(art.get("summary", ""))
    ]
    logging.info("초기 필터링 후 기사 개수 (RSS 기준): %d", len(pre_filtered_articles))

    # 6. 동시 처리로 실제 본문 가져오기 및 재필터링
    def fetch_and_filter(art):
        full_content = get_full_article_content(art.link)
        if is_relevant(art.title) or is_relevant(full_content):
            return {
                "title": art.title,
                "content": full_content,
                "url": art.link,
                "pubdate": art.pub_date_obj,
            }
        return None

    filtered_articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_and_filter, art): art for art in pre_filtered_articles
        }
        for future in tqdm(
            concurrent.futures.as_completed(futures),
            total=len(futures),
            desc="Fetching and filtering articles",
            colour="green",
        ):
            result = future.result()
            if result:
                filtered_articles.append(result)
    logging.info("최종 키워드 필터링 후 기사 개수: %d", len(filtered_articles))

    # 7. 각 기사에 대해 본문 요약 생성 (진행률: 파란색)
    for art in tqdm(filtered_articles, desc="Summarizing articles", colour="blue"):
        logging.info("Summarizing article: %s", art["title"])
        art["article_summary"] = summarize_text(art["content"])

    # 8. 두 기사 제목 간 유사도 측정 함수 (0 ~ 100)
    def calculate_similarity(title1, title2):
        prompt = (
            f"두 기사 제목의 유사도를 0에서 100까지의 숫자로 측정해줘.\n"
            f'기사 제목1: "{title1}"\n기사 제목2: "{title2}"\n'
            "답변은 순수한 숫자(예: 95)만 출력해줘."
        )
        data = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an assistant that calculates similarity percentage between two texts.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        req_headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        max_retries = 3
        retry_delay = 2  # 초

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=data,
                    headers=req_headers,
                    timeout=30,
                )
                if response.status_code != 200:
                    logging.error(
                        "OpenAI API 오류 (시도 %d/%d): %d - %s",
                        attempt + 1,
                        max_retries,
                        response.status_code,
                        response.text,
                    )
                    if attempt < max_retries - 1:
                        logging.info("%d초 후 재시도...", retry_delay)
                        time.sleep(retry_delay)
                        continue

                result = response.json()
                answer = result["choices"][0]["message"]["content"].strip()
                match = re.search(r"(\d+(\.\d+)?)", answer)
                if match:
                    similarity = float(match.group(1))
                    logging.info(
                        "유사도 계산: '%s' vs '%s' -> %.1f%%",
                        title1,
                        title2,
                        similarity,
                    )
                    return similarity
                return 0.0

            except requests.exceptions.RequestException as e:
                logging.error(
                    "네트워크 오류 (시도 %d/%d): %s", attempt + 1, max_retries, str(e)
                )
                traceback.print_exc()
                if attempt < max_retries - 1:
                    logging.info("%d초 후 재시도...", retry_delay)
                    time.sleep(retry_delay)
                else:
                    logging.error("최대 재시도 횟수 초과, 유사도 계산 실패")
                    return 0.0
        return 0.0

    # 9. 구글 뉴스에서 동일 기사(유사도 70% 이상)를 최대 3개까지 찾는 함수
    def get_google_duplicates(article):
        query = article["title"]
        encoded_query = urllib.parse.quote(query)
        google_news_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        logging.info("[DEBUG] 구글 뉴스 검색 URL: %s", google_news_url)
        search_results = feedparser.parse(google_news_url)
        duplicates = []
        for entry in tqdm(
            search_results["entries"],
            desc="Calculating similarity for duplicates",
            colour="magenta",
            leave=False,
        ):
            sim = calculate_similarity(article["title"], entry.title)
            if sim >= 70:
                duplicates.append(
                    {"title": entry.title, "url": entry.link, "similarity": sim}
                )
                if len(duplicates) >= 3:  # 최대 3개까지만 검색
                    break
        return duplicates

    # 10. 각 기사에 대해 구글 뉴스 중복 기사 목록 추가 (진행률: 노란색)
    for art in tqdm(filtered_articles, desc="Finding duplicates", colour="yellow"):
        logging.info("[DEBUG] 처리 중 기사: %s", art["title"])
        art["duplicates"] = get_google_duplicates(art)

    # 11. 최종 결과 준비 (엑셀 파일 저장을 위한 데이터 생성)
    table_headers = ["기사 제목", "기사 본문", "본문 요약", "기사 URL", "발행일"]
    for i in range(3):
        table_headers.extend(
            [f"중복기사 {i+1} 제목", f"중복기사 {i+1} URL", f"유사율 {i+1} (%)"]
        )

    rows = []
    for art in filtered_articles:
        row = [
            art["title"],
            art["content"],
            art.get("article_summary", ""),
            art["url"],
            art["pubdate"].strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for i in range(3):
            if i < len(art.get("duplicates", [])):
                dup = art["duplicates"][i]
                row.extend([dup["title"], dup["url"], f"{dup['similarity']:.1f}"])
            else:
                row.extend(["", "", ""])
        rows.append(row)

    logging.info("최종 결과 준비 완료")
    # 콘솔에 엑셀 파일 내용을 출력하는 부분은 삭제됨

    # 12. 결과를 엑셀 파일로 저장하는 함수
    def save_to_excel(data, headers, filename=None):
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"output_{timestamp}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "뉴스 데이터"
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.value = header
            cell.font = Font(bold=True)
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True
            )
        for row_idx, row_data in enumerate(data, 2):
            for col_idx, value in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.value = value
                cell.alignment = Alignment(wrap_text=True)
        for column in ws.columns:
            max_length = 0
            column_letter = openpyxl.utils.get_column_letter(column[0].column)
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = min(len(str(cell.value)), 50)
                except Exception:
                    pass
            adjusted_width = max_length + 2
            ws.column_dimensions[column_letter].width = adjusted_width
        wb.save(filename)
        logging.info("엑셀 파일 저장 완료: %s", filename)
        return filename

    # 13. 최종 결과를 엑셀로 저장하고, 파일명을 반환
    output_filename = save_to_excel(rows, table_headers)

    # 14. Gmail API를 이용해 다수의 수신자에게 메일 발송 (엑셀 파일 첨부)
    def send_email_with_attachment(subject, body, attachment_filename):
        GMAIL_SENDER_EMAIL = os.getenv("GMAIL_SENDER_EMAIL")
        GMAIL_RECIPIENTS = os.getenv("GMAIL_RECIPIENTS", "")
        recipient_list = [x.strip() for x in GMAIL_RECIPIENTS.split(",") if x.strip()]
        GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE")

        if not (
            GMAIL_CREDENTIALS_FILE
            and os.path.exists(GMAIL_CREDENTIALS_FILE)
            and GMAIL_SENDER_EMAIL
            and recipient_list
        ):
            logging.error("Gmail 발송에 필요한 설정이 누락되었습니다.")
            logging.error(
                "자격증명 파일: %s",
                (
                    "있음"
                    if GMAIL_CREDENTIALS_FILE and os.path.exists(GMAIL_CREDENTIALS_FILE)
                    else "없음"
                ),
            )
            logging.error("발신자 이메일: %s", "있음" if GMAIL_SENDER_EMAIL else "없음")
            logging.error("수신자 목록: %s", "있음" if recipient_list else "없음")
            return False

        try:
            creds = None
            try:
                creds = Credentials.from_authorized_user_info(
                    json.load(open(GMAIL_CREDENTIALS_FILE)),
                    scopes=["https://www.googleapis.com/auth/gmail.send"],
                )
            except Exception as token_err:
                logging.error(
                    "표준 형식 자격증명 로드 실패, 커스텀 형식 시도: %s", str(token_err)
                )
                with open(GMAIL_CREDENTIALS_FILE, "r") as f:
                    creds_data = json.load(f)
                required_keys = ["client_id", "client_secret", "refresh_token"]
                if not all(key in creds_data for key in required_keys):
                    logging.error(
                        "자격증명 파일에 필요한 키가 누락되었습니다: %s", required_keys
                    )
                    return False
                creds = Credentials(
                    None,
                    refresh_token=creds_data["refresh_token"],
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=creds_data["client_id"],
                    client_secret=creds_data["client_secret"],
                    scopes=["https://www.googleapis.com/auth/gmail.send"],
                )

            if creds and creds.expired and creds.refresh_token:
                logging.info("만료된 토큰 갱신 시도 중...")
                creds.refresh(Request())
                with open(GMAIL_CREDENTIALS_FILE, "w") as token_file:
                    token_data = {
                        "client_id": creds.client_id,
                        "client_secret": creds.client_secret,
                        "refresh_token": creds.refresh_token,
                        "token": creds.token,
                        "token_uri": creds.token_uri,
                        "scopes": creds.scopes,
                    }
                    json.dump(token_data, token_file)
                    logging.info("갱신된 토큰이 파일에 저장되었습니다.")

            if not creds or not creds.valid:
                logging.error("유효한 자격증명을 얻지 못했습니다.")
                return False

            service = build("gmail", "v1", credentials=creds)

            message = MIMEMultipart()
            message["to"] = ", ".join(recipient_list)
            message["from"] = GMAIL_SENDER_EMAIL
            message["subject"] = subject
            message.attach(MIMEText(body, "plain"))

            try:
                with open(attachment_filename, "rb") as f:
                    attachment_data = f.read()
                attachment_part = MIMEApplication(
                    attachment_data, Name=os.path.basename(attachment_filename)
                )
                attachment_part["Content-Disposition"] = (
                    f'attachment; filename="{os.path.basename(attachment_filename)}"'
                )
                message.attach(attachment_part)
            except Exception as attach_err:
                logging.error("첨부파일 처리 중 오류 발생: %s", str(attach_err))
                traceback.print_exc()
                return False

            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            message_body = {"raw": raw_message}

            sent_message = (
                service.users()
                .messages()
                .send(userId="me", body=message_body)
                .execute()
            )
            logging.info("이메일 발송 성공: %s", sent_message)
            return True

        except Exception as e:
            logging.error("Gmail 발송 중 오류 발생: %s", str(e))
            traceback.print_exc()
            if "invalid_grant" in str(e).lower():
                logging.error(
                    "인증 토큰이 만료되었거나 유효하지 않습니다. Google Cloud Console에서 OAuth 인증을 다시 설정하세요."
                )
            return False

    # 15. 메일 발송 정보 구성
    today_str = datetime.now().strftime("%Y-%m-%d")
    email_subject = f"YNA 뉴스 결과 - {today_str}"
    email_body = (
        f"오늘 날짜: {today_str}\n"
        f"검색 키워드: {', '.join(keyword_list)}\n"
        f"총 뉴스 기사 수: {len(filtered_articles)}개\n\n"
        "첨부된 엑셀 파일을 확인해 주세요."
    )

    # 16. 메일 발송 (환경변수 SEND_EMAIL이 true인 경우)
    if os.getenv("SEND_EMAIL", "").lower() == "true":
        logging.info("Gmail 메일 발송 시도 중...")
        email_result = send_email_with_attachment(
            email_subject, email_body, output_filename
        )
        if email_result:
            logging.info("메일 발송 완료!")
        else:
            logging.error("메일 발송에 실패했습니다. 환경 변수를 확인하세요.")
    else:
        logging.info(
            "메일 발송 기능이 비활성화되어 있습니다. .env 파일에 SEND_EMAIL=true를 추가하세요."
        )

    logging.info("프로그램 종료")


if __name__ == "__main__":
    main()
