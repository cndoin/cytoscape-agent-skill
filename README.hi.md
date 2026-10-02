# Cytoscape Agent Skill

**AI एजेंटों से Cytoscape Desktop के मूल इंजन को चलाएँ।** विश्लेषण और विज़ुअलाइज़ेशन Cytoscape ही करता है; एजेंट दस्तावेज़ित कमांड चुनता है। यह परियोजना एल्गोरिदम दोबारा लागू नहीं करती और Cytoscape की जगह NetworkX या igraph का उपयोग नहीं करती।

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md)

## सुविधाएँ

- CyREST और Commands API से नेटवर्क, तालिकाएँ, सत्र और दृश्य संसाधन आयात/निर्यात करें।
- चलाने से पहले वास्तविक कमांड और पैरामीटर खोजें; किसी भी workflow चरण की विफलता पर प्रक्रिया रुक जाती है।
- Streamable HTTP या शामिल stdio ब्रिज से MCP क्लाइंट जोड़ें।
- नौ क्लाइंट परिवारों के लिए कॉन्फ़िगरेशन और इंजन संस्करण तथा SHA-256 वाले provenance manifest बनाएँ।
- परिवेश जाँचें, इंजन खोजें और कॉन्फ़िगरेशन सुरक्षित रूप से लिखें।

## तुरंत शुरू करें

आवश्यकताएँ: Python 3.8+, Cytoscape Desktop 3.10+ और Java 17+। अतिरिक्त Python पैकेज आवश्यक नहीं हैं।

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

पिन किए गए इंजन को स्थापित करने से पहले [`INSTALL.md`](INSTALL.md) और `docs/03-跨Agent接入指南.md` पढ़ें। डाउनलोड और इंस्टॉलर स्थानीय सिस्टम बदलते हैं; पहले स्रोत और अखंडता जाँचें।

## एजेंट जोड़ें

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

पहला कमांड केवल कॉन्फ़िगरेशन दिखाता है; दूसरा उसे प्रोजेक्ट में लिखता है। मिश्रित कॉन्फ़िगरेशन फ़ाइलें अपने-आप नहीं बदली जातीं। Claude Desktop जैसे केवल stdio क्लाइंट के लिए `python scripts/cyctl.py bridge --port 1234` चलाएँ। विवरण `docs/03-跨Agent接入指南.md` में है।

## सुरक्षा और सीमाएँ

- कुछ layouts में randomness हो सकती है; provenance manifest समान coordinates की गारंटी नहीं देता।
- `system` या `attach` मोड में वास्तविक इंजन संस्करण बताएँ।
- बाइनरी सुरक्षित रखने के लिए PNG/PDF/SVG/CX को `rest ... --out FILE` से निर्यात करें।
- अप्रमाणित डाउनलोड डिफ़ॉल्ट रूप से रोके जाते हैं और जोखिमपूर्ण कमांड सुरक्षित हैं।

## सत्यापन

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

वास्तविक इंजन परीक्षणों के लिए Cytoscape और उसके ऐप्स चाहिए। `docs/07` और `docs/08` दिनांकित साक्ष्य हैं, स्थायी सार्वभौमिक गारंटी नहीं। [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) और [`INSTALL.md`](INSTALL.md) देखें।

कोड और दस्तावेज़ LGPL-2.1 ([`LICENSE`](LICENSE)) के अंतर्गत हैं। Cytoscape Desktop और उसके ऐप अलग upstream सॉफ़्टवेयर हैं और यहाँ शामिल नहीं हैं। योगदान का स्वागत है: [`CONTRIBUTING.md`](CONTRIBUTING.md)।
