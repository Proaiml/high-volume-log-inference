"""Akış halinde şablon çıkarımı (Drain algoritmasının sadeleştirilmiş hali).

Her log önce maskelenir, sonra sabit derinlikli bir ağaçta kendi uzunluğundaki ve
aynı ilk kelimelerle başlayan şablonlarla karşılaştırılır. Yeterince benzerse o
şablona katılır (farklı kelimeler <*> olur), değilse yeni şablon açılır.
Maliyet log sayısından bağımsızdır; tekrar eden maskeli satırlar önbellekten gelir.
"""


class Template:
    __slots__ = ("id", "level", "service", "tokens")

    def __init__(self, tid, level, service, tokens):
        self.id = tid
        self.level = level
        self.service = service
        self.tokens = tokens

    @property
    def text(self):
        return " ".join(self.tokens)


class TemplateMiner:
    def __init__(self, sim_threshold=0.6, depth=3, max_children=64, max_cache=100_000):
        self.sim_threshold = sim_threshold
        self.depth = depth
        self.max_children = max_children
        self.max_cache = max_cache
        self.templates = {}
        self._root = {}
        self._cache = {}
        self._next_id = 1

    def add(self, level, service, masked):
        key = (level, service, masked)
        tpl = self._cache.get(key)
        if tpl is not None:
            return tpl
        tpl = self._match(level, service, masked.split())
        if len(self._cache) >= self.max_cache:
            self._cache.clear()
        self._cache[key] = tpl
        return tpl

    def _match(self, level, service, tokens):
        node = self._root.setdefault((level, service, len(tokens)), {})
        for tok in tokens[: self.depth]:
            child = node.get(tok)
            if child is None:
                if tok != "<*>" and len(node) >= self.max_children:
                    tok = "<*>"
                child = node.setdefault(tok, {})
            node = child
        leaf = node.setdefault(None, [])

        best, best_sim, best_params = None, -1.0, -1
        n = len(tokens)
        for tpl in leaf:
            same = params = 0
            for a, b in zip(tpl.tokens, tokens):
                if a == "<*>":
                    params += 1
                elif a == b:
                    same += 1
            sim = same / n if n else 1.0
            if sim > best_sim or (sim == best_sim and params > best_params):
                best, best_sim, best_params = tpl, sim, params

        if best is not None and best_sim >= self.sim_threshold:
            merged = [a if a == b else "<*>" for a, b in zip(best.tokens, tokens)]
            if merged != best.tokens:
                best.tokens = merged
            return best

        tpl = Template(self._next_id, level, service, list(tokens))
        self._next_id += 1
        self.templates[tpl.id] = tpl
        leaf.append(tpl)
        return tpl
