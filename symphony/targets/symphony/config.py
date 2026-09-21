"""Symphony-family target options and generated image metadata."""

from dataclasses import asdict, dataclass

from ...middle.model import CompileError


@dataclass(frozen=True)
class Target:
    ram_size: int = 16 * 1024 * 1024
    persistent_size: int = 0
    load_address: int = 0
    pic: bool = False
    assume_zeroed_ram: bool = False
    isa: str = "symphony"

    def validate(self):
        for name, size in (
            ("RAM", self.ram_size),
            ("persistent memory", self.persistent_size),
        ):
            if size == 0 and name != "RAM":
                continue
            if size < 4 or size > 2**32 or size & (size - 1):
                raise CompileError(
                    f"{name} size must be a power of two from 4 to 2^32"
                )
        if not 0 <= self.load_address < 2**32:
            raise CompileError("load address must fit 32 bits")
        if self.isa not in ("dynphony", "symphony"):
            raise CompileError("ISA must be 'dynphony' or 'symphony'")

    @property
    def fixed_instruction_width(self):
        return 4 if self.isa == "symphony" else 0


@dataclass
class Image:
    binary: bytes
    symbols: dict
    target: Target
    frames: dict

    def metadata(self):
        return {
            "target": asdict(self.target),
            "image_size": len(self.binary),
            "symbols": self.symbols,
            "frame_sizes": self.frames,
            "symbol_addressing": (
                "image-relative offsets" if self.target.pic else "absolute addresses"
            ),
        }


__all__ = ["Image", "Target"]
