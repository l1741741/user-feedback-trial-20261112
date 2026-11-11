"""Token-aware rewriting of SQLite schema objects during a table rebuild.

``Table.transform()`` recreates a table under a temporary name.  Indexes,
triggers and views that reference the table are stored as raw SQL in
``sqlite_master`` and would silently break when columns are renamed or
dropped.  This module rewrites that SQL using the same small lexer as
:mod:`sqlite_utils.create_table_parser`, so column renames only touch real
SQL identifiers - string literals, comments, function names, collations,
cast targets and same-named columns of other tables are left alone.

Every rewrite is conservative: when a reference cannot be resolved
unambiguously, :class:`SchemaRewriteError` is raised and the caller's table
is left untouched instead of producing a broken object.  No third-party
SQL parser is used.
"""

from dataclasses import dataclass, field

from .create_table_parser import (
    ParseError,
    Token,
    _ascii_fold,
    _FLOAT_RE,
    _INTEGER_RE,
    _lex,
    _matching_paren,
    _meaningful,
    _quote_replacement,
    _unquote,
    is_identifier_token,
    quote_identifier,
)

# Keywords that end a FROM clause at parenthesis depth 0.
_FROM_TERMINATORS = frozenset(
    (
        "WHERE",
        "GROUP",
        "HAVING",
        "WINDOW",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "RETURNING",
    )
)
_JOIN_WORDS = frozenset(
    (
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "NATURAL",
        "OUTER",
        "ON",
        "USING",
    )
)
_COMPOUND_OPERATORS = frozenset(("UNION", "EXCEPT", "INTERSECT"))
_DML_STARTERS = frozenset(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "REPLACE"))


class SchemaRewriteError(ValueError):
    """Raised when a stored SQL object cannot be rewritten reliably."""


def _fold(text: str) -> str:
    return _ascii_fold(text)


def _is_identifier(tokens: list[Token], index: int) -> bool:
    return is_identifier_token(tokens, index)


def _is_numeric_literal(text: str) -> bool:
    return bool(
        _INTEGER_RE.fullmatch(text) or _FLOAT_RE.fullmatch(text)
    )


def _split_on(
    tokens: list[Token], start: int, end: int, delimiter: str
) -> list[tuple[int, int]]:
    """Split a token range on a punctuation token at parenthesis depth 0."""
    spans: list[tuple[int, int]] = []
    depth = 0
    span_start = start
    for index in range(start, end):
        text = tokens[index].text
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text == delimiter:
            spans.append((span_start, index))
            span_start = index + 1
    spans.append((span_start, end))
    return spans


def _find_keyword(
    tokens: list[Token], start: int, end: int, keyword: str
) -> int | None:
    depth = 0
    for index in range(start, end):
        text = tokens[index].text
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and tokens[index].is_keyword(keyword):
            return index
    return None


def _first_keyword_of(
    tokens: list[Token], start: int, end: int, keywords: frozenset[str]
) -> int | None:
    depth = 0
    for index in range(start, end):
        text = tokens[index].text
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text.upper() in keywords:
            return index
    return None


@dataclass
class Source:
    """A row source visible inside a query scope."""

    alias_folded: str
    # Folded column names exposed by this source.  None means the columns
    # cannot be enumerated (an opaque subquery or CTE).
    columns: frozenset[str] | None
    is_target: bool = False

    def exposes(self, folded_column: str) -> bool:
        return self.columns is not None and folded_column in self.columns


@dataclass
class Scope:
    sources: list[Source] = field(default_factory=list)
    # Token positions of SELECT output aliases - never column references.
    output_aliases: set[int] = field(default_factory=set)

    def find_source(self, folded_name: str) -> Source | None:
        for source in self.sources:
            if source.alias_folded == folded_name:
                return source
        return None

    def has_target(self) -> bool:
        return any(source.is_target for source in self.sources)

    def classify(self, folded_column: str) -> str:
        """Classify an unqualified identifier.

        ``"target"``: resolves to the table being transformed.
        ``"other"``: resolves to a different visible source.
        ``"unknown"``: cannot be resolved.
        """
        seen_target = False
        seen_other = False
        for source in self.sources:
            if source.exposes(folded_column):
                if source.is_target:
                    seen_target = True
                else:
                    seen_other = True
        if seen_target:
            return "target"
        if seen_other:
            return "other"
        return "unknown"


class SchemaRewriter:
    """Rewrites identifiers belonging to one table inside stored SQL."""

    def __init__(
        self,
        table_name: str,
        rename: dict[str, str],
        drop: set[str],
        table_columns: list[str],
        resolve_columns,
    ):
        self.table_folded = _fold(table_name)
        # Folded old column name -> new name (new name keeps requested casing).
        self.rename_folded = {_fold(old): new for old, new in rename.items()}
        self.drop_folded = {_fold(column) for column in drop}
        # Folded -> original casing of the table's current columns.
        self.column_casing = {
            _fold(column): column for column in table_columns
        }
        self.target_columns = frozenset(self.column_casing)
        # Callable(kind, folded_name) -> list[str] of column names, or None.
        self.resolve_columns = resolve_columns
        self.edits: list[tuple[int, int, str]] = []

    # -- public entry points -------------------------------------------------

    def rewrite_index_sql(self, sql: str, index_name: str) -> str:
        try:
            tokens = _meaningful(_lex(sql))
            self._walk_index(tokens, index_name)
        except ParseError as ex:
            raise SchemaRewriteError(
                f"Index {index_name!r} could not be parsed: {ex}"
            ) from ex
        return self._apply(sql)

    def rewrite_trigger_sql(self, sql: str, trigger_name: str) -> str:
        try:
            tokens = _meaningful(_lex(sql))
            self._walk_trigger(tokens, trigger_name)
        except ParseError as ex:
            raise SchemaRewriteError(
                f"Trigger {trigger_name!r} could not be parsed: {ex}"
            ) from ex
        return self._apply(sql)

    def rewrite_view_sql(
        self, sql: str, view_name: str
    ) -> tuple[str, bool]:
        """Return ``(rewritten_sql, changed)``."""
        try:
            tokens = _meaningful(_lex(sql))
            before = len(self.edits)
            changed = self._walk_view(tokens, view_name)
        except ParseError as ex:
            raise SchemaRewriteError(
                f"View {view_name!r} could not be parsed: {ex}"
            ) from ex
        if not changed:
            # Nothing relevant was rewritten.
            del self.edits[before:]
            return sql, False
        return self._apply(sql), True

    # -- edit primitives -----------------------------------------------------

    def _rename_token(self, token: Token) -> None:
        new_name = self.rename_folded[_fold(_unquote(token.text))]
        self.edits.append(
            (token.start, token.end, _quote_replacement(token.text, new_name))
        )

    def _dropped(self, kind: str, object_name: str, token: Token) -> None:
        raise SchemaRewriteError(
            f"Cannot drop column {_unquote(token.text)!r}: {kind} "
            f"{object_name!r} still references it"
        )

    def _unresolved(self, kind: str, object_name: str, token: Token) -> None:
        raise SchemaRewriteError(
            f"{kind.capitalize()} {object_name!r} references identifier "
            f"{_unquote(token.text)!r} that cannot be resolved against the "
            "table being transformed; refusing to rewrite it"
        )

    def _apply(self, sql: str) -> str:
        for start, end, replacement in sorted(self.edits, reverse=True):
            sql = sql[:start] + replacement + sql[end:]
        return sql

    # -- shared expression walking ------------------------------------------

    def _qualified_chain(
        self, tokens: list[Token], index: int
    ) -> tuple[list[Token], int] | None:
        # Collect only the identifier parts of a name.name[.name] chain.
        chain = [tokens[index]]
        probe = index + 1
        while (
            probe + 1 < len(tokens)
            and tokens[probe].text == "."
            and tokens[probe + 1].kind in ("word", "identifier")
        ):
            chain.append(tokens[probe + 1])
            probe += 2
        if len(chain) == 1:
            return None
        return chain, probe - 1

    def _resolve_chain(
        self,
        chain: list[Token],
        scope: Scope | None,
        kind: str,
        object_name: str,
    ) -> None:
        names = [_fold(_unquote(token.text)) for token in chain]
        if len(chain) == 2:
            table_name, column_name = names
            # OLD.col / NEW.col inside a trigger always address the table
            # whose change fired the trigger.
            if table_name in ("old", "new"):
                column_token = chain[1]
            else:
                source = scope.find_source(table_name) if scope else None
                if source is None or not source.is_target:
                    return
                column_token = chain[1]
        elif len(chain) == 3:
            schema_name, table_name, column_name = names
            if schema_name not in ("main", "temp") or table_name != self.table_folded:
                return
            column_token = chain[2]
        else:
            return
        folded = _fold(_unquote(column_token.text))
        if folded in self.drop_folded:
            self._dropped(kind, object_name, column_token)
        if folded in self.rename_folded:
            self._rename_token(column_token)

    def _walk_expression(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        kind: str,
        object_name: str,
    ) -> None:
        index = start
        while index < end:
            token = tokens[index]
            if token.text == "(":
                close = _matching_paren(tokens, index)
                inner = self._first_token(tokens, index + 1, close)
                if inner is not None and inner.text.upper() in (
                    "SELECT",
                    "WITH",
                ):
                    self._walk_query(tokens, index + 1, close, kind, object_name)
                else:
                    self._walk_expression(
                        tokens, index + 1, close, scope, kind, object_name
                    )
                index = close + 1
                continue
            if not _is_identifier(tokens, index):
                # A qualified name (table.column) has a head that is followed
                # by a dot rather than a parenthesis; handle it even when the
                # generic identifier classifier would skip it.
                if token.kind in ("word", "identifier"):
                    chain = self._qualified_chain(tokens, index)
                    if chain is not None:
                        chain_tokens, chain_end = chain
                        self._resolve_chain(
                            chain_tokens, scope, kind, object_name
                        )
                        index = chain_end + 1
                        continue
                # Keywords, punctuation and string tokens are never columns.
                index += 1
                continue
            if _is_numeric_literal(token.text):
                index += 1
                continue
            chain = self._qualified_chain(tokens, index)
            if chain is not None:
                chain_tokens, chain_end = chain
                self._resolve_chain(chain_tokens, scope, kind, object_name)
                index = chain_end + 1
                continue
            if index in scope.output_aliases:
                index += 1
                continue
            folded = _fold(_unquote(token.text))
            # A bare source name is a table reference, not a column.
            if scope.find_source(folded) is not None:
                index += 1
                continue
            verdict = scope.classify(folded)
            if verdict == "target":
                if folded in self.drop_folded:
                    self._dropped(kind, object_name, token)
                if folded in self.rename_folded:
                    self._rename_token(token)
            elif verdict == "unknown":
                self._unresolved(kind, object_name, token)
            index += 1

    @staticmethod
    def _first_token(
        tokens: list[Token], start: int, end: int
    ) -> Token | None:
        for index in range(start, end):
            if tokens[index].kind not in ("whitespace", "comment"):
                return tokens[index]
        return None

    # -- query (SELECT / WITH) walking --------------------------------------

    def _walk_query(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        kind: str,
        object_name: str,
        cte_scopes: dict[str, Scope] | None = None,
    ) -> Scope:
        cte_scopes = dict(cte_scopes or {})
        index = start
        if index < end and tokens[index].is_keyword("WITH"):
            index = self._consume_with(
                tokens, index, end, cte_scopes, kind, object_name
            )
        arms = self._split_compound(tokens, index, end)
        last_scope: Scope | None = None
        for arm_start, arm_end in arms:
            last_scope = self._walk_select(
                tokens, arm_start, arm_end, cte_scopes, kind, object_name
            )
        assert last_scope is not None
        return last_scope

    def _consume_with(
        self,
        tokens: list[Token],
        index: int,
        end: int,
        cte_scopes: dict[str, Scope],
        kind: str,
        object_name: str,
    ) -> int:
        index += 1  # WITH
        if index < end and tokens[index].is_keyword("RECURSIVE"):
            index += 1
        while index < end:
            name_token = tokens[index]
            if not _is_identifier(tokens, index):
                raise SchemaRewriteError("Malformed WITH clause")
            cte_name = _fold(_unquote(name_token.text))
            index += 1
            has_explicit_columns = False
            if index < end and tokens[index].text == "(":
                has_explicit_columns = True
                close = _matching_paren(tokens, index)
                index = close + 1
            if index >= end or not tokens[index].is_keyword("AS"):
                raise SchemaRewriteError("Malformed common table expression")
            index += 1
            if index >= end or tokens[index].text != "(":
                raise SchemaRewriteError(
                    "Common table expression is missing its query"
                )
            close = _matching_paren(tokens, index)
            cte_scope = self._walk_query(
                tokens, index + 1, close, kind, object_name, cte_scopes
            )
            # Outer queries can resolve the CTE's target-table columns; an
            # explicit column list or a CTE built from an opaque source stays
            # conservative.
            cte_columns: frozenset[str] | None = None
            if not has_explicit_columns:
                cte_columns = self._scope_target_columns(cte_scope)
            cte_scopes[cte_name] = Scope(
                sources=[
                    Source(
                        cte_name,
                        cte_columns,
                        is_target=cte_columns is not None,
                    )
                ]
            )
            index = close + 1
            if index < end and tokens[index].text == ",":
                index += 1
                continue
            break
        return index

    def _scope_target_columns(self, scope: Scope) -> frozenset[str] | None:
        """Target-table columns visible through a query scope.

        Returns ``None`` when the scope also contains opaque or non-target
        sources, in which case an unqualified name cannot be classified
        safely.
        """
        columns: frozenset[str] = frozenset()
        for source in scope.sources:
            if not source.is_target:
                return None
            if source.columns is None:
                return None
            columns = columns | source.columns
        return columns

    def _split_compound(
        self, tokens: list[Token], start: int, end: int
    ) -> list[tuple[int, int]]:
        arms: list[tuple[int, int]] = []
        depth = 0
        arm_start = start
        index = start
        while index < end:
            text = tokens[index].text
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            elif depth == 0 and text.upper() in _COMPOUND_OPERATORS:
                arms.append((arm_start, index))
                index += 1
                if index < end and tokens[index].text.upper() in ("ALL", "DISTINCT"):
                    index += 1
                arm_start = index
                continue
            index += 1
        arms.append((arm_start, end))
        return arms

    def _walk_select(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        cte_scopes: dict[str, Scope],
        kind: str,
        object_name: str,
    ) -> Scope:
        if not tokens[start].is_keyword("SELECT"):
            raise SchemaRewriteError(
                f"{kind.capitalize()} {object_name!r} contains a statement that "
                "cannot be rewritten"
            )
        scope = Scope()
        from_index = _find_keyword(tokens, start, end, "FROM")
        if from_index is not None:
            from_end = self._parse_from(
                tokens, from_index + 1, end, scope, cte_scopes, kind, object_name
            )
            self._walk_select_list(
                tokens, start + 1, from_index, scope, kind, object_name
            )
            # WHERE / GROUP BY / HAVING / WINDOW / ORDER BY / LIMIT / RETURNING
            self._walk_expression(
                tokens, from_end, end, scope, kind, object_name
            )
        else:
            # SELECT without FROM (e.g. SELECT 1) - nothing table-scoped here.
            self._walk_select_list(
                tokens, start + 1, end, scope, kind, object_name
            )
        return scope

    def _walk_select_list(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        kind: str,
        object_name: str,
    ) -> None:
        for item_start, item_end in _split_on(tokens, start, end, ","):
            meaningful_positions = [
                index
                for index in range(item_start, item_end)
                if tokens[index].kind not in ("whitespace", "comment")
            ]
            if not meaningful_positions:
                continue
            as_index = next(
                (
                    index
                    for index in range(item_start, item_end)
                    if tokens[index].is_keyword("AS")
                ),
                None,
            )
            if as_index is not None:
                scope.output_aliases.add(as_index + 1)
                expression_end = as_index
            else:
                expression_end = item_end
                # Implicit alias: a trailing identifier after an expression.
                if len(meaningful_positions) > 1:
                    last = meaningful_positions[-1]
                    if _is_identifier(tokens, last):
                        scope.output_aliases.add(last)
                        expression_end = last
            self._walk_expression(
                tokens, item_start, expression_end, scope, kind, object_name
            )

    def _parse_from(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        cte_scopes: dict[str, Scope],
        kind: str,
        object_name: str,
    ) -> int:
        index = start
        pending: Source | None = None
        while index < end:
            token = tokens[index]
            upper = token.text.upper() if token.kind == "word" else ""
            if upper in _FROM_TERMINATORS:
                break
            if upper == "ON":
                edge = self._join_edge(tokens, index + 1, end)
                self._walk_expression(
                    tokens, index + 1, edge, scope, kind, object_name
                )
                index = edge
                continue
            if upper == "USING":
                index += 1
                if index < end and tokens[index].text == "(":
                    close = _matching_paren(tokens, index)
                    self._walk_using(
                        tokens, index + 1, close, scope, kind, object_name
                    )
                    index = close + 1
                continue
            if upper in _JOIN_WORDS or token.is_keyword("AS"):
                if token.is_keyword("AS"):
                    index += 1
                    alias_token = tokens[index]
                    if pending is not None:
                        pending.alias_folded = _fold(_unquote(alias_token.text))
                        pending = None
                    index += 1
                    continue
                index += 1
                continue
            if token.text == "(":
                close = _matching_paren(tokens, index)
                inner = self._first_token(tokens, index + 1, close)
                if inner is not None and inner.text.upper() in ("SELECT", "WITH"):
                    sub_scope = self._walk_query(
                        tokens, index + 1, close, kind, object_name, cte_scopes
                    )
                    # A derived table exposes the target's columns when the
                    # subquery reads only from the target; otherwise it is
                    # treated as opaque and unqualified names are refused.
                    sub_columns = self._scope_target_columns(sub_scope)
                    source = Source(
                        "",
                        sub_columns,
                        is_target=sub_columns is not None,
                    )
                    scope.sources.append(source)
                    pending = source
                else:
                    self._parse_from(
                        tokens,
                        index + 1,
                        close,
                        scope,
                        cte_scopes,
                        kind,
                        object_name,
                    )
                index = close + 1
                continue
            if token.text == ",":
                index += 1
                continue
            if _is_identifier(tokens, index) or (
                token.kind == "word"
                and index + 1 < end
                and tokens[index + 1].text == "."
            ):
                table_name, index = self._read_table_name(tokens, index, end)
                source = self._make_source(table_name, cte_scopes)
                scope.sources.append(source)
                pending = source
                if index < end:
                    peek = tokens[index]
                    if (
                        peek.kind in ("word", "identifier")
                        and peek.text.upper()
                        not in (_JOIN_WORDS | _FROM_TERMINATORS)
                        and peek.text not in (",", "(")
                    ):
                        source.alias_folded = _fold(_unquote(peek.text))
                        pending = None
                        index += 1
                continue
            # Defensive: never spin on an unrecognized token.
            index += 1
        return index

    def _join_edge(self, tokens: list[Token], start: int, end: int) -> int:
        depth = 0
        for index in range(start, end):
            text = tokens[index].text
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            elif depth == 0 and text.upper() in (_JOIN_WORDS | _FROM_TERMINATORS):
                return index
        return end

    def _walk_using(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        kind: str,
        object_name: str,
    ) -> None:
        for index in range(start, end):
            token = tokens[index]
            if not _is_identifier(tokens, index):
                continue
            folded = _fold(_unquote(token.text))
            verdict = scope.classify(folded)
            if verdict == "unknown":
                # USING columns exist on both join sides; an unresolvable
                # name is left for SQLite to validate.
                continue
            if verdict == "target":
                if folded in self.drop_folded:
                    self._dropped(kind, object_name, token)
                self._rename_token(token)

    def _read_table_name(
        self, tokens: list[Token], index: int, end: int
    ) -> tuple[str, int]:
        name = _fold(_unquote(tokens[index].text))
        index += 1
        if index + 1 < end and tokens[index].text == ".":
            return _fold(_unquote(tokens[index + 1].text)), index + 2
        return name, index

    def _make_source(
        self, folded_name: str, cte_scopes: dict[str, Scope]
    ) -> Source:
        if folded_name in cte_scopes:
            cte_source = cte_scopes[folded_name].sources[0]
            return Source(
                folded_name, cte_source.columns, is_target=cte_source.is_target
            )
        if folded_name == self.table_folded:
            return Source(folded_name, self.target_columns, is_target=True)
        columns = self._lookup_columns(folded_name)
        return Source(folded_name, columns)

    def _lookup_columns(self, folded_table: str) -> frozenset[str] | None:
        for kind in ("table", "view"):
            try:
                columns = self.resolve_columns(kind, folded_table)
            except Exception:
                columns = None
            if columns:
                return frozenset(_fold(column) for column in columns)
        return None

    # -- CREATE INDEX --------------------------------------------------------

    def _walk_index(self, tokens: list[Token], index_name: str) -> None:
        index = self._expect(tokens, 0, "CREATE") + 1
        if index < len(tokens) and tokens[index].is_keyword("UNIQUE"):
            index += 1
        index = self._expect(tokens, index, "INDEX") + 1
        if index < len(tokens) and tokens[index].is_keyword("IF"):
            index += 3  # IF NOT EXISTS
        index += 1  # index name
        index = self._expect(tokens, index, "ON") + 1
        table_name, index = self._read_table_name(tokens, index, len(tokens))
        if table_name != self.table_folded:
            return
        if index >= len(tokens) or tokens[index].text != "(":
            raise SchemaRewriteError(f"Index {index_name!r} is malformed")
        close = _matching_paren(tokens, index)
        scope = Scope(
            sources=[Source(self.table_folded, self.target_columns, is_target=True)]
        )
        for item_start, item_end in _split_on(
            tokens, index + 1, close, ","
        ):
            self._walk_expression(
                tokens, item_start, item_end, scope, "index", index_name
            )
        if close + 1 < len(tokens):
            where = close + 1
            if not tokens[where].is_keyword("WHERE"):
                raise SchemaRewriteError(
                    f"Index {index_name!r} has unexpected SQL after its column list"
                )
            self._walk_expression(
                tokens, where + 1, len(tokens), scope, "index", index_name
            )

    @staticmethod
    def _expect(tokens: list[Token], index: int, keyword: str) -> int:
        if index >= len(tokens) or not tokens[index].is_keyword(keyword):
            raise SchemaRewriteError(f"Expected {keyword}")
        return index

    # -- CREATE TRIGGER ------------------------------------------------------

    def _trigger_scope(self) -> Scope:
        columns = self.target_columns - self.drop_folded
        return Scope(
            sources=[
                Source("old", columns, is_target=True),
                Source("new", columns, is_target=True),
            ]
        )

    def _walk_trigger(self, tokens: list[Token], trigger_name: str) -> None:
        index = self._expect(tokens, 0, "CREATE") + 1
        if index < len(tokens) and tokens[index].text.upper() in ("TEMP", "TEMPORARY"):
            index += 1
        index = self._expect(tokens, index, "TRIGGER") + 1
        if index < len(tokens) and tokens[index].is_keyword("IF"):
            index += 3
        index += 1  # trigger name
        on_index = _find_keyword(tokens, index, len(tokens), "ON")
        if on_index is None:
            raise SchemaRewriteError(f"Trigger {trigger_name!r} is missing ON")
        self._rewrite_update_of(tokens, index, on_index, trigger_name)
        index = on_index + 1
        table_name, index = self._read_table_name(tokens, index, len(tokens))
        if table_name != self.table_folded:
            return
        begin_index = _find_keyword(tokens, index, len(tokens), "BEGIN")
        if begin_index is None:
            raise SchemaRewriteError(f"Trigger {trigger_name!r} is missing BEGIN")
        when_index = _find_keyword(tokens, index, begin_index, "WHEN")
        if when_index is not None:
            self._walk_expression(
                tokens,
                when_index + 1,
                begin_index,
                self._trigger_scope(),
                "trigger",
                trigger_name,
            )
        body_end = len(tokens)
        while body_end > begin_index + 1 and (
            tokens[body_end - 1].is_keyword("END")
            or tokens[body_end - 1].text == ";"
        ):
            body_end -= 1
        for stmt_start, stmt_end in _split_on(
            tokens, begin_index + 1, body_end, ";"
        ):
            positions = [
                i
                for i in range(stmt_start, stmt_end)
                if tokens[i].kind not in ("whitespace", "comment")
            ]
            if not positions:
                continue
            if tokens[positions[0]].text.upper() not in _DML_STARTERS:
                raise SchemaRewriteError(
                    f"Trigger {trigger_name!r} contains an unsupported statement "
                    f"starting with {tokens[positions[0]].text!r}"
                )
            self._walk_dml(tokens, stmt_start, stmt_end, trigger_name)

    def _rewrite_update_of(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        trigger_name: str,
    ) -> None:
        for index in range(start, end):
            if not tokens[index].is_keyword("OF"):
                continue
            probe = index + 1
            while probe < end and tokens[probe].text.upper() not in (
                "ON",
                "BEFORE",
                "AFTER",
                "INSTEAD",
            ):
                token = tokens[probe]
                if token.text != ",":
                    folded = _fold(_unquote(token.text))
                    if folded in self.drop_folded:
                        self._dropped("trigger", trigger_name, token)
                    if folded in self.rename_folded:
                        self._rename_token(token)
                probe += 1
            return

    def _walk_dml(
        self, tokens: list[Token], start: int, end: int, trigger_name: str
    ) -> None:
        first = tokens[start]
        if first.is_keyword("SELECT") or first.is_keyword("WITH"):
            scope = self._walk_query(
                tokens, start, end, "trigger", trigger_name
            )
            self._add_pseudos(scope)
            return
        trigger_scope = self._trigger_scope()
        if first.is_keyword("INSERT") or first.text.upper() == "REPLACE":
            into = _find_keyword(tokens, start, end, "INTO")
            if into is None:
                raise SchemaRewriteError(
                    f"Trigger {trigger_name!r} has an INSERT without INTO"
                )
            table_name, index = self._read_table_name(
                tokens, into + 1, end
            )
            target = self._target_scope(table_name)
            if index < end and tokens[index].text == "(":
                close = _matching_paren(tokens, index)
                if target is not None:
                    self._walk_name_list(
                        tokens, index + 1, close, target, trigger_name
                    )
                index = close + 1
            self._walk_remainder(
                tokens,
                index,
                end,
                target,
                self._dml_expression_scope(table_name),
                trigger_name,
            )
            return
        if first.is_keyword("UPDATE"):
            index = start + 1
            if index < end and tokens[index].is_keyword("OR"):
                index += 2
            table_name, index = self._read_table_name(tokens, index, end)
            target = self._target_scope(table_name)
            set_index = _find_keyword(tokens, index, end, "SET")
            if set_index is None:
                raise SchemaRewriteError(
                    f"Trigger {trigger_name!r} has an UPDATE without SET"
                )
            stop = _first_keyword_of(
                tokens, set_index + 1, end, frozenset(("WHERE", "RETURNING"))
            ) or end
            # SET assignment targets belong to the updated table itself;
            # OLD / NEW are not in scope there.
            set_scope = target or self._table_scope(table_name, is_target=False)
            self._walk_update_set(
                tokens, set_index + 1, stop, set_scope, trigger_name
            )
            self._walk_remainder(
                tokens,
                stop,
                end,
                target,
                self._dml_expression_scope(table_name),
                trigger_name,
            )
            return
        if first.is_keyword("DELETE"):
            from_index = _find_keyword(tokens, start, end, "FROM")
            if from_index is None:
                raise SchemaRewriteError(
                    f"Trigger {trigger_name!r} has a DELETE without FROM"
                )
            table_name, after = self._read_table_name(
                tokens, from_index + 1, end
            )
            target = self._target_scope(table_name)
            self._walk_remainder(
                tokens,
                after,
                end,
                target,
                self._dml_expression_scope(table_name),
                trigger_name,
            )
            return
        raise SchemaRewriteError(
            f"Trigger {trigger_name!r} contains an unsupported statement"
        )

    def _dml_expression_scope(self, folded_table: str) -> Scope:
        """Scope for WHERE / RETURNING expressions inside a trigger statement.

        The statement's own target table is visible (as both its name and,
        when it is the transformed table, the OLD / NEW pseudo-rows), so an
        unqualified column can be classified without guessing.
        """
        if folded_table == self.table_folded:
            scope = Scope()
            scope.sources.extend(self._trigger_scope().sources)
            scope.sources.append(
                Source(
                    self.table_folded,
                    self.target_columns - self.drop_folded,
                    is_target=True,
                )
            )
            return scope
        scope = self._table_scope(folded_table, is_target=False)
        # OLD / NEW still qualify references to the transformed table.
        self._add_pseudos(scope)
        return scope

    def _target_scope(self, folded_table: str) -> Scope | None:
        if folded_table != self.table_folded:
            return None
        return self._table_scope(folded_table, is_target=True)

    def _table_scope(
        self, folded_table: str, is_target: bool
    ) -> Scope:
        if is_target:
            columns = self.target_columns - self.drop_folded
        else:
            columns = self._lookup_columns(folded_table)
        return Scope(sources=[Source(folded_table, columns, is_target=is_target)])

    def _add_pseudos(self, scope: Scope) -> None:
        for source in self._trigger_scope().sources:
            if scope.find_source(source.alias_folded) is None:
                scope.sources.append(source)

    def _walk_name_list(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        trigger_name: str,
    ) -> None:
        for index in range(start, end):
            token = tokens[index]
            if not _is_identifier(tokens, index):
                continue
            folded = _fold(_unquote(token.text))
            if folded in self.drop_folded:
                self._dropped("trigger", trigger_name, token)
            if folded in self.rename_folded:
                self._rename_token(token)

    def _walk_update_set(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        trigger_name: str,
    ) -> None:
        for assignment_start, assignment_end in _split_on(
            tokens, start, end, ","
        ):
            equals = next(
                (
                    index
                    for index in range(assignment_start, assignment_end)
                    if tokens[index].text == "="
                ),
                None,
            )
            if equals is None:
                raise SchemaRewriteError(
                    f"Trigger {trigger_name!r} has a malformed SET clause"
                )
            self._walk_expression(
                tokens, assignment_start, equals, scope, "trigger", trigger_name
            )
            self._walk_expression(
                tokens, equals + 1, assignment_end, scope, "trigger", trigger_name
            )

    def _walk_remainder(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        target: Scope | None,
        expression_scope: Scope,
        trigger_name: str,
    ) -> None:
        index = start
        while index < end:
            token = tokens[index]
            if token.text == "(":
                close = _matching_paren(tokens, index)
                inner = self._first_token(tokens, index + 1, close)
                if inner is not None and inner.text.upper() in ("SELECT", "WITH"):
                    sub_scope = self._walk_query(
                        tokens, index + 1, close, "trigger", trigger_name
                    )
                    self._add_pseudos(sub_scope)
                else:
                    self._walk_expression(
                        tokens,
                        index + 1,
                        close,
                        expression_scope,
                        "trigger",
                        trigger_name,
                    )
                index = close + 1
                continue
            if token.is_keyword("SELECT") or token.is_keyword("WITH"):
                sub_scope = self._walk_query(
                    tokens, index, end, "trigger", trigger_name
                )
                self._add_pseudos(sub_scope)
                return
            if token.is_keyword("WHERE") or token.is_keyword("RETURNING"):
                self._walk_expression(
                    tokens,
                    index + 1,
                    end,
                    expression_scope,
                    "trigger",
                    trigger_name,
                )
                return
            index += 1

    # -- CREATE VIEW ---------------------------------------------------------

    def _walk_view(self, tokens: list[Token], view_name: str) -> bool:
        index = self._expect(tokens, 0, "CREATE") + 1
        if index < len(tokens) and tokens[index].text.upper() in ("TEMP", "TEMPORARY"):
            index += 1
        index = self._expect(tokens, index, "VIEW") + 1
        if index < len(tokens) and tokens[index].is_keyword("IF"):
            index += 3
        index += 1  # view name
        if index < len(tokens) and tokens[index].text == "(":
            close = _matching_paren(tokens, index)
            index = close + 1
        if index >= len(tokens) or not tokens[index].is_keyword("AS"):
            raise SchemaRewriteError(f"View {view_name!r} is malformed")
        index += 1
        scope = self._walk_query(tokens, index, len(tokens), "view", view_name)
        if not scope.has_target():
            return False
        self._expand_stars(tokens, index, len(tokens), scope, view_name)
        return True

    def _expand_stars(
        self,
        tokens: list[Token],
        start: int,
        end: int,
        scope: Scope,
        view_name: str,
    ) -> None:
        for arm_start, arm_end in self._select_arms(tokens, start, end):
            from_index = _find_keyword(tokens, arm_start, arm_end, "FROM")
            if from_index is None:
                continue
            for item_start, item_end in _split_on(
                tokens, arm_start + 1, from_index, ","
            ):
                positions = [
                    index
                    for index in range(item_start, item_end)
                    if tokens[index].kind not in ("whitespace", "comment")
                ]
                if not positions:
                    continue
                if len(positions) == 1 and tokens[positions[0]].text == "*":
                    self._expand_bare_star(
                        tokens, positions[0], scope, view_name
                    )
                elif (
                    len(positions) == 3
                    and tokens[positions[0]].kind in ("word", "identifier")
                    and tokens[positions[1]].text == "."
                    and tokens[positions[2]].text == "*"
                ):
                    name_token = tokens[positions[0]]
                    source = scope.find_source(_fold(_unquote(name_token.text)))
                    if source is not None and source.is_target:
                        self._expand_qualified_star(
                            tokens, item_start, item_end, source, view_name
                        )

    def _select_arms(
        self, tokens: list[Token], start: int, end: int
    ) -> list[tuple[int, int]]:
        arms: list[tuple[int, int]] = []
        depth = 0
        arm_start: int | None = None
        index = start
        while index < end:
            text = tokens[index].text
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            elif depth == 0 and tokens[index].is_keyword("SELECT"):
                if arm_start is None:
                    arm_start = index
            elif depth == 0 and text.upper() in _COMPOUND_OPERATORS:
                if arm_start is not None:
                    arms.append((arm_start, index))
                    arm_start = None
            index += 1
        if arm_start is not None:
            arms.append((arm_start, end))
        return arms

    def _expand_bare_star(
        self,
        tokens: list[Token],
        star_index: int,
        scope: Scope,
        view_name: str,
    ) -> None:
        targets = [
            source
            for source in scope.sources
            if source.is_target and source.columns is not None
        ]
        if len(scope.sources) != 1 or not targets:
            raise SchemaRewriteError(
                f"View {view_name!r} uses SELECT * with a join or subquery; its "
                "columns cannot be rewritten reliably"
            )
        self.edits.append(
            (
                tokens[star_index].start,
                tokens[star_index].end,
                self._star_columns(targets[0]),
            )
        )

    def _expand_qualified_star(
        self,
        tokens: list[Token],
        item_start: int,
        item_end: int,
        source: Source,
        view_name: str,
    ) -> None:
        if source.columns is None:
            raise SchemaRewriteError(
                f"View {view_name!r} selects columns from a source whose columns "
                "cannot be enumerated"
            )
        self.edits.append(
            (
                tokens[item_start].start,
                tokens[item_end - 1].end,
                self._star_columns(source),
            )
        )

    def _star_columns(self, source: Source) -> str:
        assert source.columns is not None
        parts = []
        for folded in sorted(source.columns):
            original = self.column_casing[folded]
            new_name = self.rename_folded.get(folded, original)
            rendered = quote_identifier(new_name)
            if folded in self.rename_folded:
                # Preserve the view's historical output column name.
                rendered += f" AS {quote_identifier(original)}"
            parts.append(rendered)
        return ", ".join(parts)
