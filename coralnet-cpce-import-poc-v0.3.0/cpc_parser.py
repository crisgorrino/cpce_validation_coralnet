from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import PureWindowsPath
from typing import Iterable


class CpcParseError(ValueError):
    """Raised when a CPC file does not follow the expected CPCe structure."""


@dataclass
class CpcPoint:
    x: str
    y: str
    number_label: str
    label_id: str
    notes: str


@dataclass
class CpcFile:
    code_filepath: str
    image_filepath: str
    image_width: str
    image_height: str
    display_width: str
    display_height: str
    annotation_area: dict[str, list[str]]
    points: list[CpcPoint]
    headers: list[str] = field(default_factory=list)

    @property
    def embedded_image_name(self) -> str:
        return PureWindowsPath(self.image_filepath).name

    @classmethod
    def parse(cls, text: str) -> "CpcFile":
        stream = StringIO(text, newline="")
        reader = csv.reader(stream, delimiter=",", quotechar='"')

        def read_line(expected: int) -> list[str]:
            try:
                values = [token.strip() for token in next(reader)]
            except StopIteration as exc:
                raise CpcParseError("File seems to have too few lines.") from exc
            if len(values) != expected:
                raise CpcParseError(
                    f"Line {reader.line_num} has {len(values)} comma-separated "
                    f"tokens, but {expected} were expected."
                )
            return values

        (
            code_filepath,
            image_filepath,
            image_width,
            image_height,
            display_width,
            display_height,
        ) = read_line(6)

        annotation_area = {
            "bottom_left": read_line(2),
            "bottom_right": read_line(2),
            "top_right": read_line(2),
            "top_left": read_line(2),
        }

        count_token = read_line(1)[0]
        try:
            point_count = int(count_token)
            if point_count <= 0:
                raise ValueError
        except ValueError as exc:
            raise CpcParseError(
                f"Line {reader.line_num} is supposed to have the number of points, "
                f"but this line isn't a positive integer: {count_token}"
            ) from exc

        positions: list[tuple[str, str]] = []
        for _ in range(point_count):
            x, y = read_line(2)
            positions.append((x, y))

        points: list[CpcPoint] = []
        for index in range(point_count):
            number_label, label_id, _notes_literal, notes = read_line(4)
            x, y = positions[index]
            points.append(
                CpcPoint(
                    x=x,
                    y=y,
                    number_label=number_label,
                    label_id=label_id,
                    notes=notes,
                )
            )

        headers: list[str] = []
        for _ in range(28):
            try:
                row = next(reader)
            except StopIteration:
                break
            headers.append(row[0] if row else "")

        return cls(
            code_filepath=code_filepath,
            image_filepath=image_filepath,
            image_width=image_width,
            image_height=image_height,
            display_width=display_width,
            display_height=display_height,
            annotation_area=annotation_area,
            points=points,
            headers=headers,
        )

    @staticmethod
    def _quoted(value: object) -> str:
        return f'"{str(value).replace(chr(34), "")}"'

    def to_text(self) -> str:
        rows: list[str] = []

        def add(values: Iterable[object]) -> None:
            rows.append(",".join(str(value) for value in values))

        add(
            [
                self._quoted(self.code_filepath),
                self._quoted(self.image_filepath),
                self.image_width,
                self.image_height,
                self.display_width,
                self.display_height,
            ]
        )
        add(self.annotation_area["bottom_left"])
        add(self.annotation_area["bottom_right"])
        add(self.annotation_area["top_right"])
        add(self.annotation_area["top_left"])
        add([len(self.points)])
        for point in self.points:
            add([point.x, point.y])
        for point in self.points:
            add(
                [
                    self._quoted(point.number_label),
                    self._quoted(point.label_id),
                    self._quoted("Notes"),
                    self._quoted(point.notes),
                ]
            )
        for header in self.headers:
            add([self._quoted(header)])
        return "\r\n".join(rows) + "\r\n"
