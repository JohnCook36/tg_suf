from dataclasses import dataclass


@dataclass(frozen=True)
class NewsPost:
    peer_id: int
    message_id: int
    channel_title: str
    text: str
    published_at: str
