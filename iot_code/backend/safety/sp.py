"""Tham số thuật toán ngưỡng an toàn (SSM một chiều, robot phanh đều)."""

from dataclasses import dataclass

__all__ = ["SafetyParams"]


@dataclass(frozen=True)
class SafetyParams:
    """Bộ tham số tính khoảng cách an toàn Sp, đơn vị SI (m, s, m/s).

    - ``vh_m_s`` / ``vr_m_s``: vận tốc người / robot (m/s).
    - ``tr_s`` / ``ts_s``: thời gian phản ứng / thời gian phanh (s) — tham số robot
      giả định phần mềm, không lấy độ trễ blockchain.
    - ``c_m`` / ``zd_m`` / ``zr_m``: khoảng cách an toàn tối thiểu, dự phòng
      đo đạc, dự phòng robot (m).
    """

    vh_m_s: float
    vr_m_s: float
    tr_s: float
    ts_s: float
    c_m: float
    zd_m: float
    zr_m: float

    @property
    def sh_m(self) -> float:
        """Sh = vh * (Tr + Ts): quãng đường người đi trong thời gian phản ứng + phanh."""
        return self.vh_m_s * (self.tr_s + self.ts_s)

    @property
    def sr_m(self) -> float:
        """Sr = vr * Tr: quãng đường robot đi trong thời gian phản ứng."""
        return self.vr_m_s * self.tr_s

    @property
    def ss_m(self) -> float:
        """Ss = vr * Ts / 2: quãng đường phanh đều của robot (giảm tốc tuyến tính)."""
        return self.vr_m_s * self.ts_s / 2.0

    def components(self) -> dict[str, float]:
        """Trả về 6 thành phần của Sp theo đúng khóa trong schema assessment_v1."""
        return {
            "sh_m": self.sh_m,
            "sr_m": self.sr_m,
            "ss_m": self.ss_m,
            "c_m": self.c_m,
            "zd_m": self.zd_m,
            "zr_m": self.zr_m,
        }

    @property
    def sp_m(self) -> float:
        """Sp = Sh + Sr + Ss + C + Zd + Zr: tổng khoảng cách an toàn tối thiểu (m)."""
        c = self.components()
        return c["sh_m"] + c["sr_m"] + c["ss_m"] + c["c_m"] + c["zd_m"] + c["zr_m"]
