import os
import re
import json
import html
import time
import datetime
import pytz
import requests
import argparse
import xml.etree.ElementTree as ET
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ProtocolError
from urllib3.util.retry import Retry
from utils import setup_logger

# 设置日志记录器
logger = setup_logger()

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_USER_AGENT = "hf-daily-paper-bot/1.0 (mailto:zhugamen@gmail.com)"

# 垂直领域：标题/摘要中需匹配的关键词（OR 组合，与当日 [dp] 联检）
KEYWORDS = [
    "Interpersonal Relationship",
    "Impression",
    "Personality",
    "Face",
    "Voice",
    "Feeling",
    "Mood",
    "Social Cognition",
    "Emotion Perception",
    "Emotion Expression",
    "Emotion Experience",
    "Facial Expression",
    "Vocal Expression",
    "Cross-culture",
    "Emotion",
    "anger",
    "fear",
    "happiness",
    "surprise",
    "disgust",
    "sadness",
    "anxiety",
    "depression"
]


def _make_ncbi_session():
    """
    使用连接池 + urllib3 自动重试，降低 TLS/连接被对端 RST（如 WinError 10054）时的失败率。
    """
    retry = Retry(
        total=10,
        connect=6,
        read=6,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    tool = os.getenv("NCBI_TOOL", "hf-daily-paper-pubmed")
    email = os.getenv("NCBI_EMAIL", "anonymous@example.local")
    session.headers.update(
        {
            "User-Agent": f"{tool}/1.0 ({email})",
            "Accept-Encoding": "identity",
        }
    )
    return session


def _ncbi_get(session, url, params, timeout, label="NCBI"):
    """
    在 urllib3 重试之外再包一层：捕获连接被重置、超时等，做有限次退避重试。
    """
    max_rounds = int(os.getenv("NCBI_MANUAL_RETRIES", "5"))
    base = float(os.getenv("NCBI_MANUAL_RETRY_BASE_SEC", "1.5"))
    last_exc = None
    for round_idx in range(max_rounds):
        try:
            r = session.get(url, params=params, timeout=timeout)
            return r
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            ProtocolError,
        ) as e:
            last_exc = e
            if round_idx >= max_rounds - 1:
                raise
            wait = base * (2**round_idx)
            logger.warning(
                "%s 请求异常 (%s)，%.1fs 后进行第 %d/%d 次重试",
                label,
                e,
                wait,
                round_idx + 2,
                max_rounds,
            )
            time.sleep(wait)
    raise last_exc  # pragma: no cover


def _ncbi_post(session, url, data, timeout, label="NCBI"):
    """与 _ncbi_get 相同的手动退避重试，用于 POST（长 term 避免 GET URL 超限）。"""
    max_rounds = int(os.getenv("NCBI_MANUAL_RETRIES", "5"))
    base = float(os.getenv("NCBI_MANUAL_RETRY_BASE_SEC", "1.5"))
    last_exc = None
    for round_idx in range(max_rounds):
        try:
            r = session.post(url, data=data, timeout=timeout)
            return r
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            ProtocolError,
        ) as e:
            last_exc = e
            if round_idx >= max_rounds - 1:
                raise
            wait = base * (2**round_idx)
            logger.warning(
                "%s POST 异常 (%s)，%.1fs 后进行第 %d/%d 次重试",
                label,
                e,
                wait,
                round_idx + 2,
                max_rounds,
            )
            time.sleep(wait)
    raise last_exc  # pragma: no cover


def _ncbi_common_params():
    """NCBI 推荐附带 tool / email；api_key 提高速率上限。"""
    params = {
        "tool": os.getenv("NCBI_TOOL", "hf-daily-paper-pubmed"),
        "email": os.getenv("NCBI_EMAIL", "anonymous@example.local"),
    }
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return params


def _strip_ns(tag):
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _local_name(elem):
    return _strip_ns(elem.tag) if elem is not None else ""


def _text_join(parts):
    return " ".join(p for p in parts if p and p.strip()).strip()


def _parse_pubmed_xml_batch(xml_bytes):
    """
    解析 efetch 返回的 PubMed XML（可能含多篇 PubmedArticle），
    返回 list[dict]: {pmid, title, abstract, authors: [{name}], published_at}
    """
    root = ET.fromstring(xml_bytes)
    results = []

    for article in root:
        if _local_name(article) != "PubmedArticle":
            continue

        pmid = None
        title = ""
        abstract_parts = []
        authors_out = []
        published_at = ""

        medline = None
        pubmed_data = None
        for child in article:
            ln = _local_name(child)
            if ln == "MedlineCitation":
                medline = child
            elif ln == "PubmedData":
                pubmed_data = child

        if medline is not None:
            for mc in medline:
                ln = _local_name(mc)
                if ln == "PMID":
                    pmid = (mc.text or "").strip()
                elif ln == "Article":
                    for ac in mc:
                        aln = _local_name(ac)
                        if aln == "ArticleTitle":
                            title = "".join(ac.itertext()).strip()
                        elif aln == "Abstract":
                            for ab in ac:
                                if _local_name(ab) == "AbstractText":
                                    label = ab.attrib.get("Label")
                                    chunk = "".join(ab.itertext()).strip()
                                    if chunk:
                                        if label:
                                            abstract_parts.append(f"{label}: {chunk}")
                                        else:
                                            abstract_parts.append(chunk)
                        elif aln == "AuthorList":
                            for auth in ac:
                                if _local_name(auth) != "Author":
                                    continue
                                collective = None
                                last = fore = initials = ""
                                for ael in auth:
                                    an = _local_name(ael)
                                    if an == "CollectiveName":
                                        collective = (ael.text or "").strip()
                                    elif an == "LastName":
                                        last = (ael.text or "").strip()
                                    elif an == "ForeName":
                                        fore = (ael.text or "").strip()
                                    elif an == "Initials":
                                        initials = (ael.text or "").strip()
                                if collective:
                                    name = collective
                                else:
                                    name = _text_join([fore or initials, last])
                                if name:
                                    authors_out.append({"name": name})

        # 优先从 PubmedData / History 取电子化发表日期，其次 MedlineCitation 内日期
        def parse_pub_date_elem(elem):
            if elem is None:
                return ""
            year = month = day = ""
            for el in elem:
                n = _local_name(el)
                if n == "Year":
                    year = (el.text or "").strip()
                elif n == "Month":
                    t = (el.text or "").strip()
                    month = t
                elif n == "Day":
                    day = (el.text or "").strip()
            if not year:
                return ""
            # 月份可能是数字或 JAN/FEB
            month_norm = month
            if month and not month.isdigit():
                mmap = {
                    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
                    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
                    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
                }
                month_norm = mmap.get(month[:3].upper(), "01")
            elif month.isdigit():
                month_norm = month.zfill(2)
            else:
                month_norm = "01"
            day_norm = day.zfill(2) if day.isdigit() else "01"
            try:
                datetime.date(int(year), int(month_norm), int(day_norm))
                return f"{year}-{month_norm}-{day_norm}"
            except ValueError:
                return year

        if pubmed_data is not None:
            for pd in pubmed_data:
                if _local_name(pd) != "History":
                    continue
                for hp in pd:
                    if _local_name(hp) != "PubMedPubDate":
                        continue
                    status = hp.attrib.get("PubStatus", "")
                    if status in ("pubmed", "medline", "entrez"):
                        d = parse_pub_date_elem(hp)
                        if d:
                            published_at = d
                            break
                if published_at:
                    break

        if medline is not None and not published_at:
            for mc in medline:
                if _local_name(mc) != "Article":
                    continue
                for ac in mc:
                    aln = _local_name(ac)
                    if aln == "ArticleDate":
                        published_at = parse_pub_date_elem(ac) or published_at
                    elif aln == "Journal":
                        for jc in ac:
                            if _local_name(jc) == "JournalIssue":
                                for ji in jc:
                                    if _local_name(ji) == "PubDate":
                                        published_at = parse_pub_date_elem(ji) or published_at

        abstract = "\n".join(abstract_parts).strip()

        if not pmid:
            continue

        results.append(
            {
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": authors_out,
                "published_at": published_at,
            }
        )

    return results


def _esearch_pubmed(session, date_str, retmax=10000):
    """按日期 + 标题/摘要关键词检索 PubMed；retmax 默认 10000。"""
    keyword_query = " OR ".join([f'"{kw}"[Title/Abstract]' for kw in KEYWORDS])
    term = f'"{date_str}"[dp] AND ({keyword_query})'
    url = f"{EUTILS_BASE}/esearch.fcgi"
    params = {
        **_ncbi_common_params(),
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(retmax),
    }
    r = _ncbi_post(session, url, params, timeout=120, label="esearch")
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as e:
        raise requests.RequestException(f"无法解析 esearch JSON: {e}") from e
    idlist = data.get("esearchresult", {}).get("idlist") or []
    return idlist


def _efetch_pubmed_xml_single(session, pmids_chunk):
    """单次 efetch（一批 PMID）；使用 POST 避免 id 列表过长。"""
    url = f"{EUTILS_BASE}/efetch.fcgi"
    params = {
        **_ncbi_common_params(),
        "db": "pubmed",
        "id": ",".join(pmids_chunk),
        "retmode": "xml",
    }
    r = _ncbi_post(session, url, params, timeout=180, label="efetch")
    r.raise_for_status()
    return r.content


def _efetch_pubmed_parsed(session, pmids):
    """对大量 PMID 分批 efetch 并解析合并（与 retmax 增大配套）。"""
    if not pmids:
        return []
    chunk_size = int(os.getenv("NCBI_EFETCH_BATCH_SIZE", "250"))
    inter = float(os.getenv("NCBI_INTER_REQUEST_DELAY_SEC", "0.35"))
    out = []
    for i in range(0, len(pmids), chunk_size):
        chunk = pmids[i : i + chunk_size]
        xml_bytes = _efetch_pubmed_xml_single(session, chunk)
        out.extend(_parse_pubmed_xml_batch(xml_bytes))
        if i + chunk_size < len(pmids) and inter > 0:
            time.sleep(inter)
    return out


def _strip_crossref_abstract(raw):
    """去除 Crossref 摘要中的 JATS/XML 标签，并做 HTML 实体反转义。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", str(raw))
    return html.unescape(text).strip()


def _fetch_crossref(date_str, retmax=80):
    """
    从 Crossref REST API 拉取指定 index-date 起入库、含摘要的文献，映射为与 PubMed 一致的结构，并标记 source=Crossref。
    retmax 在此作为 rows 上限，最大 100，避免单次请求过大。
    """
    crossref_query = " ".join([f'"{kw}"' for kw in KEYWORDS])
    filter_str = f"from-index-date:{date_str},has-abstract:true"
    params = {
        "query": crossref_query,
        "filter": filter_str,
        "rows": min(int(retmax), 100),
        "mailto": "zhugamen@gmail.com",
    }
    headers = {"User-Agent": CROSSREF_USER_AGENT}
    r = requests.get(
        CROSSREF_WORKS_URL,
        params=params,
        headers=headers,
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()
    items = (payload.get("message") or {}).get("items") or []
    out = []
    for item in items:
        doi = (item.get("DOI") or "").strip()
        titles = item.get("title") or []
        title = (titles[0] if titles else "").strip()
        raw_abs = item.get("abstract")
        if raw_abs is None:
            continue
        if isinstance(raw_abs, list):
            raw_abs = " ".join(str(x) for x in raw_abs)
        summary = _strip_crossref_abstract(raw_abs)
        authors_out = []
        for a in item.get("author") or []:
            if not isinstance(a, dict):
                continue
            name = f"{a.get('given', '') or ''} {a.get('family', '') or ''}".strip()
            if name:
                authors_out.append({"name": name})
        if not doi or not title or not summary:
            continue
        if not authors_out:
            authors_out.append({"name": "Unknown"})
        out.append(
            {
                "paper": {
                    "id": doi,
                    "title": title,
                    "summary": summary,
                    "authors": authors_out,
                    "publishedAt": date_str,
                    "source": "Crossref",
                }
            }
        )
    return out


def download_papers(date_str=None, retmax=10000):
    """
    从 PubMed（NCBI E-utilities）与 Crossref 下载指定日期的论文元数据，合并后映射为统一 JSON 结构。

    Args:
        date_str: 可选，指定要下载的日期，格式为 YYYY-MM-DD。如果不指定，则使用北京时间当前日期。
        retmax: PubMed esearch 返回的最大 PMID 条数，默认 10000；Crossref 条数由环境变量 CROSSREF_ROWS 控制（默认 80，上限 100）。
    """
    target_date = None
    try:
        retmax = int(retmax)
        if retmax < 1:
            retmax = 1
        if retmax > 100000:
            logger.warning("retmax 超过 100000，已截断为 100000（贴近 NCBI esearch 实务上限）")
            retmax = 100000

        if date_str is None:
            beijing_tz = pytz.timezone("Asia/Shanghai")
            current_time = datetime.datetime.now(beijing_tz)
            target_date = current_time.strftime("%Y-%m-%d")
        else:
            target_date = date_str

        logger.info(
            f"正在获取 {target_date} 的论文数据：PubMed（retmax={retmax}）+ Crossref"
        )

        pubmed_papers = []
        inter_delay = float(os.getenv("NCBI_INTER_REQUEST_DELAY_SEC", "0.35"))
        session = _make_ncbi_session()
        try:
            try:
                pmids = _esearch_pubmed(session, target_date, retmax=retmax)
                logger.info(f"PubMed esearch 返回 PMID 数量: {len(pmids)}")
                if pmids:
                    if inter_delay > 0:
                        time.sleep(inter_delay)
                    parsed = _efetch_pubmed_parsed(session, pmids)
                    by_pmid = {p["pmid"]: p for p in parsed if p.get("pmid")}
                    for pmid in pmids:
                        rec = by_pmid.get(pmid)
                        if not rec:
                            continue
                        pubmed_papers.append(
                            {
                                "paper": {
                                    "id": rec["pmid"],
                                    "title": rec["title"],
                                    "summary": rec["abstract"],
                                    "authors": rec["authors"],
                                    "publishedAt": rec["published_at"] or target_date,
                                    "source": "PubMed",
                                }
                            }
                        )
            except requests.RequestException as e:
                logger.error(f"PubMed 请求失败（将仅使用 Crossref 若可用）: {e}")
            except ET.ParseError as e:
                logger.error(f"解析 PubMed XML 失败: {e}")
        finally:
            session.close()

        crossref_rows = int(os.getenv("CROSSREF_ROWS", "80"))
        crossref_papers = []
        try:
            crossref_papers = _fetch_crossref(target_date, retmax=crossref_rows)
            logger.info(f"Crossref 返回条目数量: {len(crossref_papers)}")
        except Exception as e:
            logger.warning(f"Crossref 抓取失败（忽略该源）: {e}")

        papers = pubmed_papers + crossref_papers
        if not papers:
            logger.warning(f"{target_date} PubMed 与 Crossref 均无可用论文数据")
            return {"status": "no_data", "date": target_date}

        logger.info(
            f"合并后原始条目: PubMed {len(pubmed_papers)} 篇 + Crossref {len(crossref_papers)} 篇 = {len(papers)} 篇"
        )

        valid_papers = []
        skipped_papers = []

        for paper in papers:
            paper_info = paper.get("paper", {})
            paper_id = paper_info.get("id", "unknown")

            is_valid = True
            reasons = []

            if not paper_info:
                is_valid = False
                reasons.append("缺少paper字段")
            else:
                if not paper_info.get("title"):
                    is_valid = False
                    reasons.append("缺少标题")
                if not paper_info.get("summary"):
                    is_valid = False
                    reasons.append("缺少摘要")
                if not paper_info.get("id"):
                    is_valid = False
                    reasons.append("缺少ID")
                if not paper_info.get("authors"):
                    is_valid = False
                    reasons.append("缺少作者信息")
                if not paper_info.get("publishedAt"):
                    is_valid = False
                    reasons.append("缺少发布时间")

            if is_valid:
                valid_papers.append(paper)
                logger.debug(f"接受论文: {paper_id}")
            else:
                skipped_papers.append({"id": paper_id, "reasons": reasons})
                logger.warning(f"跳过论文 {paper_id}: {', '.join(reasons)}")

        logger.info(f"原始数据: {len(papers)}篇")
        logger.info(f"有效论文: {len(valid_papers)}篇")
        logger.info(f"跳过论文: {len(skipped_papers)}篇")

        if skipped_papers:
            logger.info("跳过的论文详情:")
            for skip_info in skipped_papers:
                logger.info(f"  ID: {skip_info['id']}, 原因: {', '.join(skip_info['reasons'])}")

        if valid_papers:
            os.makedirs("Paper_metadata_download", exist_ok=True)
            output_file = os.path.join("Paper_metadata_download", f"{target_date}.json")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(valid_papers, f, ensure_ascii=False, indent=2)
            logger.info(f"成功保存 {len(valid_papers)} 篇有效论文数据到文件")
            return {"status": "success", "date": target_date}
        logger.warning(f"{target_date} 没有有效的论文数据")
        return {"status": "no_data", "date": target_date}

    except Exception as e:
        logger.error(f"下载论文数据时发生错误: {str(e)}")
        return {"status": "error", "date": target_date or date_str, "message": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从 PubMed 与 Crossref 下载每日论文元数据")
    parser.add_argument("--date", type=str, help="指定要下载的日期 (YYYY-MM-DD格式)")
    parser.add_argument(
        "--retmax",
        type=int,
        default=10000,
        help="esearch 返回的最大 PMID 数量（默认 10000）",
    )
    args = parser.parse_args()

    result = download_papers(args.date, retmax=args.retmax)
    logger.info(f"下载结果: {result}")
    if result["status"] == "error":
        exit(1)
    exit(0)
