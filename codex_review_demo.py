"""Isolated sample for trying a Codex code review.

This file is not imported by the digital photo frame application.
"""

def next_photo(photos: list[str], current_index: int) -> str:
    """Return the photo following the current index, wrapping at the end."""
    next_index = (current_index + 1) % len(photos)
    return photos[next_index]


if __name__ == "__main__":
    print(next_photo(["a.jpg", "b.jpg"], 0))
