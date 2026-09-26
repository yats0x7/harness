from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    delimiter: str = ","
    quotechar: str = '"'
    strip_unquoted: bool = True
    comment: str = "#"

    def __post_init__(self):
        if len(self.delimiter) != 1:
            raise ValueError("delimiter must be a single character")
        if len(self.quotechar) != 1:
            raise ValueError("quotechar must be a single character")
        if self.delimiter == self.quotechar:
            raise ValueError("delimiter and quotechar must differ")


CSV = Dialect()
TSV = Dialect(delimiter="\t")
PIPE = Dialect(delimiter="|")
