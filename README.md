# Tin nóng AI

Trang tin dùng riêng: AI tự quét tin nóng thế giới 2 lần mỗi ngày, lọc theo hai chuyên mục (AI & công nghệ; Thời sự & kinh tế thế giới), chấm điểm theo mục tiêu của bạn rồi viết thẻ tóm tắt tiếng Việt có ghi nguồn.

Hiện đang ở **giai đoạn 1: kiểm chứng**. Chạy vài lượt, bạn chấm khoảng 30 tin trên trang "Bàn chấm tin". Khi AI chấm khớp ý bạn từ 75% trở lên thì mới xây trang web thật và cho chạy tự động.

## Một lượt chạy làm gì
```
Thu thập (RSS, Google News, Hacker News, Reddit)
  → Lọc trùng (không dùng AI)
  → Chấm điểm từng bài (Haiku 5.5)
  → Gộp các bài cùng sự kiện (so khớp tiêu đề, rồi Haiku)
  → Tính điểm cuối = 0,5 × quan trọng + 0,3 × ảnh hưởng + 0,2 × độ nóng
  → Viết thẻ cho sự kiện từ 7 điểm (Haiku 5.5)
  → Xuất trang "Bàn chấm tin"
```

## Cài đặt lần đầu
```bash
python3 -m venv .venv
```
```bash
.venv/bin/pip install -r requirements.txt
```
Cần biến môi trường `ANTHROPIC_API_KEY` (lấy trong Anthropic Console). Nhớ **đặt giới hạn chi 10 USD/tháng** trong Console.

## Các lệnh
| Việc | Lệnh |
|---|---|
| Kiểm tra nguồn nào còn sống | `.venv/bin/python -m pipeline.collect --check` |
| Chạy thử 30 bài, không ghi dữ liệu thật | `.venv/bin/python -m pipeline.main --dry-run --limit 30` |
| Chạy một lượt thật và xuất trang chấm | `.venv/bin/python -m pipeline.main --preview out/preview.html` |
| Nhập phiếu chấm đã xuất từ trang | `.venv/bin/python -m pipeline.ratings data/ratings_export.json` |
| Đo độ khớp AI và bạn (miễn phí) | `.venv/bin/python -m pipeline.eval --offline` |
| Đo lại sau khi sửa câu lệnh (tốn vài xu) | `.venv/bin/python -m pipeline.eval` |
| Chạy kiểm thử | `.venv/bin/python -m pytest -q` |

## Muốn AI chấm theo ý mình hơn thì sửa ở đâu
| Muốn đổi | Sửa file |
|---|---|
| Bạn là ai, mục tiêu, tài sản đầu tư, những gì không muốn đọc | `config/profile.md` (các dòng **[BẠN ĐIỀN]**) |
| Nguồn tin, từ khóa Google News | `config/sources.yaml` |
| Mô hình AI, ngưỡng điểm, trọng số, ngân sách | `config/settings.yaml` |
| Thang chấm, luật gộp, cách viết thẻ | `pipeline/prompts/*.md` |

Sau mỗi lần sửa câu lệnh, chạy `python -m pipeline.eval` để chắc chắn độ khớp không tụt.

## Context Engineering nằm ở đâu
| Phần | Nằm ở |
|---|---|
| Hướng dẫn hệ thống | `config/profile.md` + `pipeline/prompts/*.md` |
| Nhiệm vụ cốt lõi | mỗi bước một nhiệm vụ: `classify.py`, `cluster.py`, `summarize.py` |
| Bộ nhớ & trạng thái | SQLite `data/tin.db` (bài, sự kiện, phản hồi), `tests/golden.json` |
| Kiến thức bổ sung | nội dung bài báo bóc từ đường dẫn gốc lúc viết thẻ (không lưu toàn văn) |
| Công cụ | `feedparser`, `trafilatura`, API Hacker News, Google News RSS |
| Định dạng đầu ra | khuôn Pydantic trong `pipeline/models.py` + `client.messages.parse` |

## Chi phí ước tính
Khoảng 2 USD/tháng với 2 lượt/ngày. Mỗi lần gọi AI đều được ghi vào bảng `llm_calls`. Khi chi phí từ đầu tháng vượt 9 USD, trang tự bỏ bước viết thẻ và chỉ giữ bước chấm điểm.

## An toàn
- AI không được khuyên mua hay bán mã cổ phiếu hoặc tiền số cụ thể. Thẻ có nói tới tài sản luôn kèm dòng "Không phải lời khuyên đầu tư".
- Nội dung bài báo được coi là dữ liệu, không phải lệnh cho AI.
- Không lưu toàn văn bài báo, chỉ lưu tiêu đề, mô tả ngắn và đường dẫn.
