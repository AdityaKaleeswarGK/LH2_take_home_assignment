# Golden solution — pr-117

## Provenance

- **kind**: `commit`
- **commit_sha**: `12e05553d33bf80384668fbc0b67dc84814d906d`
- **base_sha**: `690716746cae8e6caac35979a61c598df73595f8`
- **pull_request**: `117`
- **url**: `https://github.com/mahmoud/glom/pull/117`
- **merged_at**: `2019-11-13T20:19:41Z`

## Why this is the correct fix

This is the change the repository itself made, taken from git rather than written by hand. It is *verified* correct rather than assumed, by four measurements recorded in `evidence/`:

1. **Fail-before.** 9 designated test(s) do not pass against `input/`. 9 of them ran and failed for a behavioural reason — an assertion, or an exception raised from inside the repository — rather than an import or collection error.
2. **Pass-after.** The same tests pass against `solution/`.
3. **No collateral breakage.** Every test passing before the change still passes after it.
4. **Determinism.** The verdict was reproduced across 2 fresh container runs with identical statuses and identical failure signatures.

### Designated tests

- `glom.test.test_basic::test_invoke`
- `glom.test.test_check::test_check_basic`
- `glom.test.test_fill::test`
- `glom.test.test_mutation::test_assign`
- `glom.test.test_path_and_t::test_path_t_roundtrip`
- `glom.test.test_reduction::test_flatten`
- `glom.test.test_reduction::test_fold`
- `glom.test.test_reduction::test_sum_integers`
- `glom.test.test_streaming::test_filter`

## Diff

```diff
diff --git a/glom/core.py b/glom/core.py
index 6736fe2..fe03e28 100644
--- a/glom/core.py
+++ b/glom/core.py
@@ -32,7 +32,7 @@ from collections import OrderedDict
 
 from boltons.typeutils import make_sentinel
 from boltons.iterutils import is_iterable
-from boltons.funcutils import format_invocation
+#from boltons.funcutils import format_invocation
 
 PY2 = (sys.version_info[0] == 2)
 if PY2:
@@ -248,6 +248,57 @@ class UnregisteredTarget(GlomError):
         return msg
 
 
+if getattr(__builtins__, '__dict__', None) is not None:
+    # pypy's __builtins__ is a module, as is CPython's REPL, but at
+    # normal execution time it's a dict?
+    __builtins__ = __builtins__.__dict__
+
+
+_BUILTIN_ID_NAME_MAP = dict([(id(v), k)
+                             for k, v in __builtins__.items()])
+
+def bbrepr(obj):
+    """A better repr for builtins, when the built-in repr isn't
+    roundtrippable.
+    """
+    ret = repr(obj)
+    if not ret.startswith('<'):
+        return ret
+    return _BUILTIN_ID_NAME_MAP.get(id(obj), ret)
+
+
+# TODO: push this back up to boltons with repr kwarg
+def format_invocation(name='', args=(), kwargs=None, **kw):
+    """Given a name, positional arguments, and keyword arguments, format
+    a basic Python-style function call.
+
+    >>> print(format_invocation('func', args=(1, 2), kwargs={'c': 3}))
+    func(1, 2, c=3)
+    >>> print(format_invocation('a_func', args=(1,)))
+    a_func(1)
+    >>> print(format_invocation('kw_func', kwargs=[('a', 1), ('b', 2)]))
+    kw_func(a=1, b=2)
+
+    """
+    _repr = kw.pop('repr', repr)
+    if kw:
+        raise TypeError('unexpected keyword args: %r' % ', '.join(kw.keys()))
+    kwargs = kwargs or {}
+    a_text = ', '.join([_repr(a) for a in args])
+    if isinstance(kwargs, dict):
+        kwarg_items = [(k, kwargs[k]) for k in sorted(kwargs)]
+    else:
+        kwarg_items = kwargs
+    kw_text = ', '.join(['%s=%s' % (k, _repr(v)) for k, v in kwarg_items])
+
+    all_args_text = a_text
+    if all_args_text and kw_text:
+        all_args_text += ', '
+    all_args_text += kw_text
+
+    return '%s(%s)' % (name, all_args_text)
+
+
 class Path(object):
     """Path objects specify explicit paths when the default
     ``'a.b.c'``-style general access syntax won't work or isn't
@@ -454,7 +505,7 @@ class Literal(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return '%s(%r)' % (cn, self.value)
+        return '%s(%s)' % (cn, bbrepr(self.value))
 
 
 class Spec(object):
@@ -498,8 +549,8 @@ class Spec(object):
     def __repr__(self):
         cn = self.__class__.__name__
         if self.scope:
-            return '%s(%r, scope=%r)' % (cn, self.spec, self.scope)
-        return '%s(%r)' % (cn, self.spec)
+            return '%s(%s, scope=%r)' % (cn, bbrepr(self.spec), self.scope)
+        return '%s(%s)' % (cn, bbrepr(self.spec))
 
 
 class Coalesce(object):
@@ -607,7 +658,7 @@ class Coalesce(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return format_invocation(cn, self.subspecs, self._orig_kwargs)
+        return format_invocation(cn, self.subspecs, self._orig_kwargs, repr=bbrepr)
 
 
 class Inspect(object):
@@ -772,7 +823,7 @@ class Call(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return '%s(%r, args=%r, kwargs=%r)' % (cn, self.func, self.args, self.kwargs)
+        return '%s(%s, args=%r, kwargs=%r)' % (cn, bbrepr(self.func), self.args, self.kwargs)
 
 
 def _is_spec(obj, strict=False):
@@ -907,6 +958,7 @@ class Invoke(object):
         :meth:`~Invoke.specs()` and other :class:`Invoke`
         methods may be called multiple times, just remember that every
         call returns a new spec.
+
         """
         ret = self.__class__(self.func)
         ret._args = self._args + ('S', a, kw)
@@ -944,29 +996,31 @@ class Invoke(object):
         return ret
 
     def __repr__(self):
-        chunks = [self.__class__.__name__]
+        base_fname = self.__class__.__name__
         fname_map = {'C': 'constants', 'S': 'specs', '*': 'star'}
         if type(self.func) is Spec:
-            chunks.append('.specfunc({!r})'.format(self.func.spec))
+            base_fname += '.specfunc'
+            args = (self.func.spec,)
         else:
-            chunks.append('({!r})'.format(self.func))
+            args = (self.func,)
+        chunks = [format_invocation(base_fname, args, repr=bbrepr)]
+
         for i in range(len(self._args) // 3):
-            op, args, kwargs = self._args[i * 3: i * 3 + 3]
+            op, args, _kwargs = self._args[i * 3: i * 3 + 3]
             fname = fname_map[op]
-            chunks.append('.{}('.format(fname))
             if op in ('C', 'S'):
-                chunks.append(', '.join(
-                    [repr(a) for a in args] +
-                    ['{}={!r}'.format(k, v) for k, v in kwargs.items()
-                     if self._cur_kwargs[k] is kwargs]))
+                kwargs = [(k, v) for k, v in _kwargs.items()
+                          if self._cur_kwargs[k] is _kwargs]
             else:
+                kwargs = {}
                 if args:
-                    chunks.append('args=' + repr(args))
-                if args and kwargs:
-                    chunks.append(", ")
-                if kwargs:
-                    chunks.append('kwargs=' + repr(kwargs))
-            chunks.append(')')
+                    kwargs['args'] = args
+                if _kwargs:
+                    kwargs['kwargs'] = _kwargs
+                args = ()
+
+            chunks.append('.' + format_invocation(fname, args, kwargs, repr=bbrepr))
+
         return ''.join(chunks)
 
     def glomit(self, target, scope):
@@ -1169,24 +1223,6 @@ UP = make_sentinel('UP')
 ROOT = make_sentinel('ROOT')
 
 
-def _format_invocation(name='', args=(), kwargs=None):  # pragma: no cover
-    # TODO: add to boltons
-    kwargs = kwargs or {}
-    a_text = ', '.join([repr(a) for a in args])
-    if isinstance(kwargs, dict):
-        kwarg_items = kwargs.items()
-    else:
-        kwarg_items = kwargs
-    kw_text = ', '.join(['%s=%r' % (k, v) for k, v in kwarg_items])
-
-    star_args_text = a_text
-    if star_args_text and kw_text:
-        star_args_text += ', '
-    star_args_text += kw_text
-
-    return '%s(%s)' % (name, star_args_text)
-
-
 class Let(object):
     """
     This specifier type assigns variables to the scope.
@@ -1208,14 +1244,10 @@ class Let(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return _format_invocation(cn, kwargs=self._binding)
+        return format_invocation(cn, kwargs=self._binding, repr=bbrepr)
 
 
 def _format_t(path, root=T):
-    def kwarg_fmt(kw):
-        if isinstance(kw, str):
-            return kw
-        return repr(kw)
     prepr = ['T' if root is T else 'S']
     i = 0
     while i < len(path):
@@ -1223,12 +1255,10 @@ def _format_t(path, root=T):
         if op == '.':
             prepr.append('.' + arg)
         elif op == '[':
-            prepr.append("[%r]" % (arg,))
+            prepr.append("[%s]" % (bbrepr(arg),))
         elif op == '(':
             args, kwargs = arg
-            prepr.append("(%s)" % ", ".join([repr(a) for a in args] +
-                                            ["%s=%r" % (kwarg_fmt(k), v)
-                                             for k, v in kwargs.items()]))
+            prepr.append(format_invocation(args=args, kwargs=kwargs, repr=bbrepr))
         elif op == 'P':
             return _format_path(path)
         i += 2
@@ -1435,7 +1465,7 @@ class Check(object):
     def __repr__(self):
         cn = self.__class__.__name__
         posargs = (self.spec,) if self.spec is not T else ()
-        return format_invocation(cn, posargs, self._orig_kwargs)
+        return format_invocation(cn, posargs, self._orig_kwargs, repr=bbrepr)
 
 
 class Auto(object):
@@ -1966,7 +1996,7 @@ class Fill(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        rpr = '' if self.spec is None else repr(self.spec)
+        rpr = '' if self.spec is None else bbrepr(self.spec)
         return '%s(%s)' % (cn, rpr)
 
 
diff --git a/glom/mutation.py b/glom/mutation.py
index d02dfd4..3ac96ec 100644
--- a/glom/mutation.py
+++ b/glom/mutation.py
@@ -5,7 +5,7 @@ import operator
 from pprint import pprint
 
 from .core import Path, T, S, Spec, glom, UnregisteredTarget, GlomError, PathAccessError, UP
-from .core import TType, register_op, TargetRegistry
+from .core import TType, register_op, TargetRegistry, bbrepr
 
 try:
     basestring
@@ -13,7 +13,7 @@ except NameError:
     basestring = str
 
 
-if getattr(__builtins__, '__dict__', None):
+if getattr(__builtins__, '__dict__', None) is not None:
     # pypy's __builtins__ is a module, as is CPython's REPL, but at
     # normal execution time it's a dict?
     __builtins__ = __builtins__.__dict__
@@ -206,7 +206,7 @@ class Assign(object):
         cn = self.__class__.__name__
         if self.missing is None:
             return '%s(%r, %r)' % (cn, self._orig_path, self.val)
-        return '%s(%r, %r, missing=%r)' % (cn, self._orig_path, self.val, self.missing)
+        return '%s(%r, %r, missing=%s)' % (cn, self._orig_path, self.val, bbrepr(self.missing))
 
 
 def assign(obj, path, val, missing=None):
diff --git a/glom/reduction.py b/glom/reduction.py
index fcbcb01..d87123a 100644
--- a/glom/reduction.py
+++ b/glom/reduction.py
@@ -5,7 +5,7 @@ from pprint import pprint
 
 from boltons.typeutils import make_sentinel
 
-from .core import TargetRegistry, Path, T, glom, GlomError, UnregisteredTarget
+from .core import TargetRegistry, Path, T, glom, GlomError, UnregisteredTarget, format_invocation, bbrepr
 
 _MISSING = make_sentinel('_MISSING')
 
@@ -96,7 +96,10 @@ class Fold(object):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return '%s(%r, init=%r, op=%r)' % (cn, self.subspec, self.init, self.op)
+        kwargs = {'init': self.init}
+        if self.op is not operator.iadd:
+            kwargs['op'] = self.op
+        return format_invocation(cn, (self.subspec,), kwargs, repr=bbrepr)
 
 
 class Sum(Fold):
@@ -122,7 +125,9 @@ class Sum(Fold):
 
     def __repr__(self):
         cn = self.__class__.__name__
-        return '%s(%r, init=%r)' % (cn, self.subspec, self.init)
+        args = () if self.subspec is T else (self.subspec,)
+        kwargs = {'init': self.init} if self.init is not int else {}
+        return format_invocation(cn, args, kwargs, repr=bbrepr)
 
 
 class Flatten(Fold):
@@ -153,9 +158,13 @@ class Flatten(Fold):
 
     def __repr__(self):
         cn = self.__class__.__name__
+        args = () if self.subspec is T else (self.subspec,)
+        kwargs = {}
         if self.lazy:
-            return '%s(%r, init="lazy")' % (cn, self.subspec)
-        return '%s(%r, init=%r)' % (cn, self.subspec, self.init)
+            kwargs['init'] = 'lazy'
+        elif self.init is not list:
+            kwargs['init'] = self.init
+        return format_invocation(cn, args, kwargs, repr=bbrepr)
 
 
 def flatten(target, **kwargs):
diff --git a/glom/streaming.py b/glom/streaming.py
index f24fcd2..5ed1b2f 100644
--- a/glom/streaming.py
+++ b/glom/streaming.py
@@ -18,7 +18,7 @@ except ImportError:
 from boltons.iterutils import split_iter, chunked_iter, windowed_iter, unique_iter, first
 from boltons.funcutils import FunctionBuilder
 
-from .core import glom, T, STOP, SKIP, Check, _MISSING, Path, TargetRegistry, Call, Spec, S
+from .core import glom, T, STOP, SKIP, Check, _MISSING, Path, TargetRegistry, Call, Spec, S, bbrepr, format_invocation
 
 
 class Iter(object):
@@ -62,29 +62,21 @@ class Iter(object):
         return
 
     def __repr__(self):
-        chunks = [self.__class__.__name__]
+        base_args = ()
         if self.subspec != T:
-            chunks.append('({!r})'.format(self.subspec))
-        else:
-            chunks.append('()')
+            base_args = (self.subspec,)
+        base = format_invocation(self.__class__.__name__, base_args, repr=bbrepr)
+        chunks = [base]
         for fname, args, _ in reversed(self._iter_stack):
             meth = getattr(self, fname)
             fb = FunctionBuilder.from_func(meth)
             fb.args = fb.args[1:]  # drop self
             arg_names = fb.get_arg_names()
             # TODO: something fancier with defaults:
-            chunks.append("." + fname)
-            if len(args) == 0:
-                chunks.append("()")
-            elif len(arg_names) == 1:
-                assert len(args) == 1
-                chunks.append('({!r})'.format(args[0]))
-            elif arg_names:
-                chunks.append('({})'.format(", ".join([
-                    '{}={!r}'.format(name, val) for name, val in zip(arg_names, args)])))
-            else:
-                # p much just slice bc no kwargs
-                chunks.append('({})'.format(", ".join(['%s' % a for a in args])))
+            kwargs = []
+            if len(args) > 1 and arg_names:
+                args, kwargs = (), zip(arg_names, args)
+            chunks.append('.' + format_invocation(fname, args, kwargs, repr=bbrepr))
         return ''.join(chunks)
 
     def glomit(self, target, scope):
@@ -386,5 +378,5 @@ class First(object):
     def __repr__(self):
         cn = self.__class__.__name__
         if self._default is None:
-            return '%s(%r)' % (cn, self._spec)
-        return '%s(%r, default=%r)' % (cn, self._spec, self._default)
+            return '%s(%s)' % (cn, bbrepr(self._spec))
+        return '%s(%s, default=%s)' % (cn, bbrepr(self._spec), bbrepr(self._default))
diff --git a/glom/test/test_basic.py b/glom/test/test_basic.py
index 90dd314..37faeb2 100644
--- a/glom/test/test_basic.py
+++ b/glom/test/test_basic.py
@@ -181,8 +181,10 @@ def test_abstract_iterable():
     class MyIterable(object):
         def __iter__(self):
             return iter([1, 2, 3])
+    mi = MyIterable()
+    assert list(mi) == [1, 2, 3]
 
-    assert isinstance(MyIterable(), glom_core._AbstractIterable)
+    assert isinstance(mi, glom_core._AbstractIterable)
 
 
 def test_call_and_target():
@@ -231,6 +233,9 @@ def test_invoke():
         ).constants(3, b='b').specs(c='c'
         ).star(args='args2', kwargs='kwargs')
     repr(spec)  # no exceptions
+    assert repr(Invoke(len).specs(T)) == 'Invoke(len).specs(T)'
+    assert (repr(Invoke.specfunc(next).constants(len).constants(1))
+            == 'Invoke.specfunc(next).constants(len).constants(1)')
     assert glom(data, spec) == 'test'
     assert args == [
         (1, 2, 3, 4, 5),
@@ -441,6 +446,6 @@ def test_api_repr():
         if not callable(getattr(v, 'glomit', None)):
             continue
         if v.__repr__ is object.__repr__:
-            spec_types_wo_reprs.append(k)
+            spec_types_wo_reprs.append(k)  # pragma: no cover
 
     assert set(spec_types_wo_reprs) == set([])
diff --git a/glom/test/test_check.py b/glom/test/test_check.py
index 9917af9..9082cce 100644
--- a/glom/test/test_check.py
+++ b/glom/test/test_check.py
@@ -24,6 +24,8 @@ def test_check_basic():
     assert repr(Check()) == 'Check()'
     assert repr(Check(T.a)) == 'Check(T.a)'
     assert repr(Check(equal_to=1)) == 'Check(equal_to=1)'
+    assert repr(Check(instance_of=dict)) == 'Check(instance_of=dict)'
+    assert repr(Check(T(len), validate=sum)) == 'Check(T(len), validate=sum)'
 
     target = [1, 'a']
     assert glom(target, [Check(type=str, default=SKIP)]) == ['a']
diff --git a/glom/test/test_fill.py b/glom/test/test_fill.py
index b50f114..1fcd47a 100644
--- a/glom/test/test_fill.py
+++ b/glom/test/test_fill.py
@@ -16,3 +16,5 @@ def test():
 
     assert repr(Fill()) == 'Fill()'
     assert repr(Fill(T)) == 'Fill(T)'
+
+    assert repr(Fill(len)) == 'Fill(len)'
diff --git a/glom/test/test_mutation.py b/glom/test/test_mutation.py
index 01932d4..13fa01d 100644
--- a/glom/test/test_mutation.py
+++ b/glom/test/test_mutation.py
@@ -30,7 +30,9 @@ def test_assign():
         Assign(T, 1)
 
     assert repr(Assign(T.a, 1)) == 'Assign(T.a, 1)'
-    assert repr(Assign(T.a, 1, missing=dict)).startswith('Assign(T.a, 1, missing=<')
+    assign_spec = Assign(T.a, 1, missing=dict)
+    assert repr(assign_spec) == "Assign(T.a, 1, missing=dict)"
+    assert repr(assign_spec) == repr(eval(repr(assign_spec)))
 
 
 def test_assign_spec_val():
diff --git a/glom/test/test_path_and_t.py b/glom/test/test_path_and_t.py
index 5b73fb7..f231a5d 100644
--- a/glom/test/test_path_and_t.py
+++ b/glom/test/test_path_and_t.py
@@ -45,6 +45,10 @@ def test_path_t_roundtrip():
     # check that multiple nested paths reduce
     assert repr(Path(Path(Path('a')))) == "Path('a')"
 
+    # check builtin repr
+    assert repr(T[len]) == 'T[len]'
+    assert repr(T.func(len, sum)) == 'T.func(len, sum)'
+
 
 def test_path_access_error_message():
 
diff --git a/glom/test/test_reduction.py b/glom/test/test_reduction.py
index 1bb31cb..e1fcadb 100644
--- a/glom/test/test_reduction.py
+++ b/glom/test/test_reduction.py
@@ -1,4 +1,6 @@
 
+import operator
+
 import pytest
 from boltons.dictutils import OMD
 
@@ -22,7 +24,8 @@ def test_sum_integers():
     target = target + [{}]  # add a non-compliant dict
     assert glom(target, Sum([Coalesce('num', default=0)])) ==4
 
-    repr(Sum())
+    assert repr(Sum()) == 'Sum()'
+    assert repr(Sum(len, init=float)) == 'Sum(len, init=float)'
 
 
 def test_sum_seqs():
@@ -48,7 +51,8 @@ def test_fold():
 
     assert glom(target, Fold(T, lambda: 1, op=lambda l, r: l * r)) == 24
 
-    repr(Fold(T, int))
+    assert repr(Fold(T, int)) == 'Fold(T, init=int)'
+    assert repr(Fold(T, int, op=operator.imul)).startswith('Fold(T, init=int, op=<')
 
     # signature coverage
     with pytest.raises(TypeError):
@@ -82,8 +86,9 @@ def test_flatten():
     assert next(gen) == 1
     assert list(gen) == [2, 3]
 
-    repr(Flatten())
-    repr(Flatten(init='lazy'))
+    assert repr(Flatten()) == 'Flatten()'
+    assert repr(Flatten(init='lazy')) == "Flatten(init='lazy')"
+    assert repr(Flatten(init=tuple)) == "Flatten(init=tuple)"
 
 
 def test_flatten_func():
diff --git a/glom/test/test_streaming.py b/glom/test/test_streaming.py
index bc69159..18f94c5 100644
--- a/glom/test/test_streaming.py
+++ b/glom/test/test_streaming.py
@@ -51,14 +51,15 @@ def test_filter():
     out = glom(imags, spec)
     assert out == [0j, 2j]
 
-    assert repr(Iter().filter(T.a.b)).startswith('Iter().filter(T.a.b)')
+    assert repr(Iter().filter(T.a.b)) == 'Iter().filter(T.a.b)'
+    assert repr(Iter(list).filter(sum)) == 'Iter(list).filter(sum)'
 
 
 def test_map():
     spec = Iter().map(lambda x: x * 2)
     out = glom(RANGE_5, spec)
     assert list(out) == [0, 2, 4, 6, 8]
-    assert repr(Iter().map(T.a.b)).startswith('Iter().map(T.a.b)')
+    assert repr(Iter().map(T.a.b)) == 'Iter().map(T.a.b)'
 
 
 def test_split_flatten():
diff --git a/glom/test/test_target_types.py b/glom/test/test_target_types.py
index 0d977c5..36399a2 100644
--- a/glom/test/test_target_types.py
+++ b/glom/test/test_target_types.py
@@ -68,13 +68,10 @@ def test_types_bare():
 
     # check again that registering object for 'get' doesn't change the
     # fact that we don't have iterate support yet
-    try:
+    with pytest.raises(UnregisteredTarget) as exc_info:
         glommer.glom({'test': [{'hi': 'hi'}]}, ('test', ['hi']))
-    except UnregisteredTarget as ute:
-        # feel free to update the "(at ['test'])" part to improve path display
-        assert str(ute) == "target type 'list' not registered for 'iterate', expected one of registered types: (dict) (at ['test'])"
-    else:
-        assert False, 'expected an UnregisteredTarget exception'
+    # feel free to update the "(at ['test'])" part to improve path display
+    assert str(exc_info.value) == "target type 'list' not registered for 'iterate', expected one of registered types: (dict) (at ['test'])"
     return
 
 
@@ -98,7 +95,7 @@ def test_exact_register():
     assert value == expected
 
     with pytest.raises(UnregisteredTarget):
-        glommer.glom(list(range(3)), [lambda x: x * 2])
+        glommer.glom(list(range(3)), ['unused'])
 
     return
 
@@ -199,7 +196,7 @@ def test_faulty_op_registration():
     treg = TargetRegistry()
 
     with pytest.raises(TypeError, match="text name, not:"):
-        treg.register_op(None, lambda t: False)
+        treg.register_op(None, len)
     with pytest.raises(TypeError, match="callable, not:"):
         treg.register_op('fake_op', object())
 
diff --git a/glom/test/test_tutorial.py b/glom/test/test_tutorial.py
index 84dc4b8..98490af 100644
--- a/glom/test/test_tutorial.py
+++ b/glom/test/test_tutorial.py
@@ -16,3 +16,5 @@ def test_tutorial():
     assert res == val
 
     contact = Contact('Julian', emails=[Email('julian@sunnyvaletrailerpark.info')])
+    contact.save()
+    assert Contact.objects.get(contact_id=contact.id) is contact
```
