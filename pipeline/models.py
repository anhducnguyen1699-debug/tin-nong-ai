"""Khuôn dữ liệu.

- `Article`: một bài báo thu thập được (chưa qua AI).
- Các lớp Pydantic còn lại là khuôn JSON bắt AI trả về (structured output).
  Không đặt ràng buộc min/max trong khuôn; mã sẽ tự kẹp giá trị về đúng khoảng.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["ai_cong_nghe", "thoi_su_kinh_te", "bo_qua"]
Goal = Literal["dau_tu", "nghe_y", "dia_chinh_tri"]

GOAL_LABELS = {
    "dau_tu": "Đầu tư",
    "nghe_y": "Thu nhập nghề y nhờ AI",
    "dia_chinh_tri": "Địa chính trị & kinh tế",
}
CATEGORY_LABELS = {
    "ai_cong_nghe": "AI & công nghệ",
    "thoi_su_kinh_te": "Thời sự & kinh tế",
    "bo_qua": "Bỏ qua",
}


@dataclass
class Article:
    url: str
    title: str
    source: str
    summary: str = ""
    lang: str = "en"
    published_at: datetime | None = None
    hn_points: int = 0
    reddit_score: int = 0
    origin: str = "rss"  # rss | google_news | hacker_news | reddit
    extra: dict = field(default_factory=dict)


# ---------- Bước chấm điểm ----------
class ArticleScore(BaseModel):
    id: int = Field(description="Số thứ tự của bài trong danh sách được đưa vào")
    category: Category
    importance: int = Field(description="Mức quan trọng 1–10 theo thang có mốc")
    impact_dau_tu: int = Field(description="Ảnh hưởng tới mục tiêu Đầu tư, 0–10")
    impact_nghe_y: int = Field(description="Ảnh hưởng tới mục tiêu Tăng thu nhập nghề y nhờ AI, 0–10")
    impact_dia_chinh_tri: int = Field(description="Ảnh hưởng tới mục tiêu Hiểu địa chính trị & kinh tế, 0–10")
    impact_viet_nam: int = Field(description="Mức tác động tới Việt Nam, 0–10")
    reason: str = Field(description="Một câu tiếng Việt, tối đa 25 từ, giải thích điểm")


class ClassifyResult(BaseModel):
    items: list[ArticleScore]


# ---------- Bước gộp sự kiện ----------
class ClusterDecision(BaseModel):
    article_id: int
    event_id: int = Field(description="Mã sự kiện có sẵn mà bài thuộc về; 0 nếu là sự kiện mới")
    new_group: str = Field(
        description="Nếu event_id = 0: nhãn ngắn cho sự kiện mới, các bài cùng sự kiện mới dùng chung nhãn. "
        "Nếu event_id > 0: để chuỗi rỗng"
    )


class ClusterResult(BaseModel):
    decisions: list[ClusterDecision]


# ---------- Bước viết thẻ ----------
class KeyPoint(BaseModel):
    text: str = Field(description="Một ý chính, tối đa 35 từ, viết lại bằng lời của bạn")
    source_id: int = Field(description="Mã nguồn (thuộc tính id của thẻ <nguon>) chứa thông tin này")


class EventCard(BaseModel):
    title_vi: str = Field(description="Tiêu đề tiếng Việt, tối đa 15 từ, nói rõ chuyện gì xảy ra")
    key_points: list[KeyPoint] = Field(description="Đúng 3 ý chính")
    why_it_matters: str = Field(description="Vì sao quan trọng với người đọc, 1–2 câu, gắn với mục tiêu cụ thể")
    what_to_do: str = Field(description="Nên làm gì trong tuần này, 1–2 việc cụ thể")
    affected_goals: list[Goal]
    is_investment_related: bool = Field(description="True nếu thẻ có bàn tới tài sản hoặc thị trường")
