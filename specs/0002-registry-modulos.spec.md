# SPEC-0002 — Registry de modulos

> **Status:** aceito · **Depende de:** SPEC-0000, SPEC-0001 · **Normativo.**

## 1. Objetivo

Descobrir, registrar e instanciar building blocks sem que o nucleo os conheca em
tempo de compilacao.

## 2. `lukato/modules/registry.py`

```python
class ModuleRegistry:
    def register(self, module_cls: type[BaseModule]) -> type[BaseModule]
    def unregister(self, slug: str) -> None
    def get(self, slug: str) -> type[BaseModule]            # ModuleNotFound
    def instantiate(self, slug: str) -> BaseModule          # cache por slug
    def all(self) -> list[type[BaseModule]]
    def describe(self) -> list[ModuleDescriptor]
    def discover(self, entry_point_group: str = "lukato.modules") -> int
    def load_builtin(self) -> int
    def clear(self) -> None                                 # usado em testes

registry: ModuleRegistry
def register_module(cls: type[BaseModule]) -> type[BaseModule]
```

`ModuleDescriptor` (pydantic): `slug`, `name`, `kind`, `version`, `description`,
`capabilities`, `config_schema`, `default_binding`, `source` (`"builtin"|"entry_point"`),
`class_path`, `ui` (`UIDescriptor`).

## 3. Regras

1. `register` valida: subclasse de `BaseModule`, `slug` no padrao
   `^[a-z0-9][a-z0-9-]{1,62}$`, `kind` valido, `handle` implementado.
   Slug repetido → `ConflictError` (a menos que `replace=True`).
2. `discover` usa `importlib.metadata.entry_points(group=...)`. Falha ao carregar um
   entry point **nao** derruba a descoberta: registra WARNING, conta como falha e segue.
3. `instantiate` mantem uma instancia por slug (`dict[str, BaseModule]`) — modulos
   devem ser stateless entre requisicoes; estado por requisicao vive em `ModuleRequest`.
4. O registry e um singleton de processo, mas `clear()` + fixture permitem isolamento
   nos testes.
5. `describe()` alimenta `GET /api/v1/registry` e a pagina `/registry` do console.

## 4. Criterios de aceite

* Registrar uma classe com slug duplicado levanta `ConflictError`.
* Um entry point quebrado nao impede o boot; aparece em `discover_errors`.
* `GET /api/v1/registry` reflete exatamente `registry.describe()`.
