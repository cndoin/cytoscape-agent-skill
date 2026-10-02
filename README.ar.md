# مهارة Cytoscape للوكلاء

**شغّل محرّك Cytoscape Desktop الأصلي من خلال وكلاء الذكاء الاصطناعي.** ينفّذ Cytoscape التحليل والتصوير؛ ويختار الوكيل الأوامر الموثّقة. لا يعيد هذا المشروع تنفيذ الخوارزميات ولا يستبدل Cytoscape بمكتبات أخرى.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [हिन्दी](README.hi.md)

## الميزات

- استيراد الشبكات والجداول والجلسات والموارد المرئية وتصديرها عبر CyREST وCommands API.
- اكتشاف الأوامر والمعاملات الفعلية قبل التنفيذ، وإيقاف سير العمل عند فشل أي خطوة.
- ربط عملاء MCP عبر Streamable HTTP أو جسر stdio المضمّن.
- إنشاء إعدادات لتسع فئات من العملاء وسجلّات منشأ تتضمن إصدار المحرك وبصمات SHA-256.
- فحص البيئة واكتشاف المحرك وكتابة الإعدادات بأمان.

## البدء السريع

المتطلبات: Python 3.8+ وCytoscape Desktop 3.10+ وJava 17+. لا تحتاج أداة Python إلى حزم خارجية إضافية.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

قبل تثبيت المحرك ذي الإصدار المحدد، اقرأ [`INSTALL.md`](INSTALL.md) و`docs/03-跨Agent接入指南.md`. التنزيلات والمثبّتات تغيّر البيئة المحلية؛ تحقّق من المصدر وسلامة الملفات أولاً.

## ربط وكيل

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

يعرض الأمر الأول الإعدادات فقط، بينما يكتبها الثاني في المشروع. لا تُعدّل ملفات الإعدادات المختلطة تلقائياً. للعملاء الذين يدعمون stdio فقط مثل Claude Desktop، استخدم `python scripts/cyctl.py bridge --port 1234`. التفاصيل في `docs/03-跨Agent接入指南.md`.

## السلامة والحدود

- قد تتضمن بعض التخطيطات عشوائية؛ ولا يضمن سجلّ المنشأ تطابق الإحداثيات.
- عند استخدام وضعي `system` أو `attach`، اذكر إصدار المحرك الفعلي.
- صدّر PNG/PDF/SVG/CX باستخدام `rest ... --out FILE` للحفاظ على البيانات الثنائية.
- تُرفض التنزيلات غير القابلة للتحقق افتراضياً وتُحمى الأوامر الخطرة.

## التحقق

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

تتطلب اختبارات المحرك الحقيقي Cytoscape وتطبيقاته. التقريرَان `docs/07` و`docs/08` أدلة مؤرخة وليسا ضماناً دائماً. راجع [`SKILL.md`](cytoscape-agent-skill/SKILL.md) و[`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) و[`INSTALL.md`](INSTALL.md).

الكود والوثائق بترخيص LGPL-2.1 ([`LICENSE`](LICENSE)). برنامج Cytoscape وتطبيقاته مشاريع مستقلة من المصدر ولا تتضمنها هذه المستودع. المساهمات مرحّب بها: [`CONTRIBUTING.md`](CONTRIBUTING.md).
