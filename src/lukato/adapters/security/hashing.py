"""Derivacao e verificacao de senhas com bcrypt 5.0 (SPEC-0006 secao 3).

`bcrypt` trunca silenciosamente qualquer entrada acima de **72 bytes** — duas senhas
longas que compartilhem os primeiros 72 bytes gerariam o mesmo hash. Para eliminar a
armadilha, toda senha passa antes por um SHA-256 em hexadecimal: o resultado tem
sempre 64 caracteres ASCII, cabe folgado no limite e preserva a entropia da senha
original inteira.

`verify` nunca levanta: hash malformado, vazio, truncado ou de outro algoritmo
devolve `False`. Um erro de formato de credencial armazenada nao pode virar 500 na
rota de login — vira apenas uma falha de autenticacao.

Importar este modulo nao faz I/O; nenhum segredo e registrado em log.
"""

from __future__ import annotations

import hashlib
from typing import Final

import bcrypt

from lukato.config import get_logger
from lukato.domain.errors import ValidationError

__all__ = ["DEFAULT_ROUNDS", "MAX_ROUNDS", "MIN_ROUNDS", "BcryptHasher", "prehash"]

_logger = get_logger(__name__)

DEFAULT_ROUNDS: Final[int] = 12
"""Custo padrao exigido pela SPEC-0006 (2^12 iteracoes)."""

MIN_ROUNDS: Final[int] = 4
"""Piso aceito pelo proprio bcrypt; util apenas para acelerar testes."""

MAX_ROUNDS: Final[int] = 16
"""Teto pratico: acima disto o login passa de um segundo por tentativa."""

_ENCODING: Final[str] = "utf-8"


def prehash(password: str) -> bytes:
    """Reduz a senha a 64 bytes ASCII (SHA-256 hex) antes do bcrypt."""
    return hashlib.sha256(password.encode(_ENCODING)).hexdigest().encode("ascii")


class BcryptHasher:
    """Implementa `PasswordHasherPort` sobre bcrypt puro (sem passlib)."""

    def __init__(self, rounds: int = DEFAULT_ROUNDS) -> None:
        """Fixa o custo do bcrypt; valores fora de `[MIN_ROUNDS, MAX_ROUNDS]` sao recusados."""
        value = int(rounds)
        if not MIN_ROUNDS <= value <= MAX_ROUNDS:
            raise ValidationError(
                f"custo bcrypt invalido: {value}; use um valor entre {MIN_ROUNDS} e {MAX_ROUNDS}",
                details={"rounds": value, "min": MIN_ROUNDS, "max": MAX_ROUNDS},
            )
        self._rounds = value

    @property
    def rounds(self) -> int:
        """Custo configurado do bcrypt."""
        return self._rounds

    def hash(self, password: str) -> str:
        """Devolve o hash bcrypt da senha (pre-reduzida por SHA-256)."""
        salt = bcrypt.gensalt(rounds=self._rounds)
        return bcrypt.hashpw(prehash(password), salt).decode("ascii")

    def verify(self, password: str, hashed: str) -> bool:
        """True quando a senha corresponde ao hash; hash invalido devolve `False`."""
        candidate = hashed.strip() if hashed else ""
        if not candidate:
            return False
        try:
            return bcrypt.checkpw(prehash(password), candidate.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            _logger.warning(
                "password_hash_malformed",
                error=type(exc).__name__,
                hash_length=len(candidate),
            )
            return False

    def needs_rehash(self, hashed: str) -> bool:
        """True quando o hash existente foi gerado com custo diferente do atual."""
        parts = hashed.split("$") if hashed else []
        expected = 4
        if len(parts) < expected or not parts[2].isdigit():
            return True
        return int(parts[2]) != self._rounds
