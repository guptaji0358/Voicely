#include "GlStage.h"

#include <QCursor>
#include <QTimer>
#include <QtMath>
#include <algorithm>
#include <cmath>

namespace {

// Shaders use no #version line (GLSL 1.10 on desktop, ES 2.0 otherwise): attribute/varying/gl_FragColor only.
const char *kPrelude = "#ifdef GL_ES\nprecision highp float;\n#endif\n";

const char *kVertex = R"(
attribute vec2 a_pos;          // unit quad 0..1
uniform vec4 u_r;              // target rect in NDC: x0 y0 x1 y1
varying vec2 v_uv;
void main() {
    v_uv = a_pos;
    gl_Position = vec4(mix(u_r.xy, u_r.zw, a_pos), 0.0, 1.0);
}
)";

const char *kBackdrop = R"(
varying vec2 v_uv;
uniform vec2 u_res;
uniform float u_time, u_level, u_energy, u_radius;
uniform vec2 u_mouse;
uniform vec3 u_accent;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
}
float fbm(vec2 p) {
    float s = 0.0, a = 0.5;
    for (int i = 0; i < 4; i++) { s += a * noise(p); p *= 2.02; a *= 0.5; }
    return s;
}

void main() {
    vec2 px = v_uv * u_res;
    vec2 half_ = u_res * 0.5;
    vec2 q = abs(px - half_) - (half_ - vec2(u_radius));
    float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - u_radius;
    float alpha = 1.0 - smoothstep(-1.0, 0.5, d);

    float asp = u_res.x / u_res.y;
    vec2 uv = v_uv;
    vec2 p = (uv - 0.5) * vec2(asp, 1.0);
    vec2 par = u_mouse - 0.5;
    float e = u_energy;

    vec3 top = vec3(0.075, 0.085, 0.125);
    vec3 bottom = vec3(0.030, 0.035, 0.060);
    vec3 col = mix(bottom, top, uv.y);

    // aurora ribbons
    float t = u_time * (0.05 + 0.04 * e);
    vec2 w = p + par * 0.06;
    float a1 = fbm(vec2(w.x * 1.1 + t * 2.0, w.y * 1.5 - t) + fbm(w * 2.0 + t));
    float band = smoothstep(0.34, 0.92, a1) * smoothstep(0.05, 0.75, uv.y);
    vec3 violet = vec3(0.62, 0.30, 1.0);
    vec3 aur = mix(u_accent, violet, smoothstep(0.1, 0.9, uv.x + 0.25 * sin(u_time * 0.1)));
    col += aur * band * (0.30 + 0.20 * e + 0.30 * u_level);

    // soft colour pools that drift against the cursor
    col += u_accent * 0.16 * exp(-length(p - vec2(-0.45 * asp, 0.22) - par * 0.25) * 2.1);
    col += violet * 0.12 * exp(-length(p - vec2(0.42 * asp, -0.12) + par * 0.30) * 1.9);

    // perspective grid floor
    float horizon = 0.40;
    if (uv.y < horizon) {
        float z = horizon - uv.y;
        float depth = 0.16 / z;
        vec2 g = vec2((p.x + par.x * 0.12) * depth * 1.15 + 0.5, depth - u_time * (0.30 + 0.5 * e));
        float dx = abs(fract(g.x + 0.5) - 0.5), dy = abs(fract(g.y + 0.5) - 0.5);
        float th = 0.012 + 0.012 * depth;
        float line = (1.0 - smoothstep(0.0, th, dx)) + (1.0 - smoothstep(0.0, th, dy));
        float fade = smoothstep(0.0, 0.28, z) * (1.0 - smoothstep(0.55, 1.0, z * 1.6));
        col += mix(u_accent, vec3(1.0), 0.15) * line * 0.22 * fade * (1.0 + 1.6 * u_level);
    }

    // parallax particles (three depth layers)
    for (int i = 0; i < 3; i++) {
        float fi = float(i);
        float sc = 16.0 + fi * 13.0;
        vec2 gp = (uv * vec2(asp, 1.0) + par * (0.015 + 0.02 * fi)) * sc + vec2(0.0, u_time * 0.12 * (1.0 + fi * 0.6));
        vec2 id = floor(gp);
        vec2 fp = fract(gp) - 0.5;
        float h = hash(id + fi * 17.0);
        vec2 off = (vec2(hash(id + 3.1), hash(id + 7.7)) - 0.5) * 0.6;
        float r = length(fp - off);
        float tw = 0.5 + 0.5 * sin(u_time * (1.0 + h * 3.0) + h * 40.0);
        float s = step(0.83, h) * smoothstep(0.11, 0.0, r) * tw;
        col += mix(u_accent, vec3(1.0), 0.65) * s * (0.55 - 0.13 * fi);
    }

    col *= 1.0 - 0.42 * dot(p * 0.85, p * 0.85);       // vignette
    gl_FragColor = vec4(col * alpha, alpha);
}
)";

const char *kOrb = R"(
varying vec2 v_uv;
uniform float u_time, u_level, u_energy;
uniform vec3 u_accent;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
}

void main() {
    vec2 p = (v_uv * 2.0 - 1.0) / 0.40;           // sphere radius = 0.40 of the quad, the rest is halo
    float r = length(p);
    float pulse = 1.0 + 0.10 * u_level + 0.05 * sin(u_time * 3.0) * u_energy;
    r /= pulse;
    float halo = exp(-max(r - 1.0, 0.0) * 2.6) * (0.30 + 0.35 * u_energy + 0.5 * u_level);
    vec4 outc = vec4(u_accent * halo * 0.55, halo * 0.55);
    if (r < 1.0) {
        float z = sqrt(1.0 - r * r);
        vec3 n = normalize(vec3(p / pulse, z));
        float a = u_time * (0.6 + 0.8 * u_energy);
        mat2 R = mat2(cos(a), -sin(a), sin(a), cos(a));
        vec2 q = R * n.xy;
        float sw = noise(q * 3.0 + vec2(0.0, u_time * 0.4) + n.z * 2.0) * 0.6 + noise(q * 6.0 - u_time * 0.3) * 0.4;
        vec3 deep = vec3(0.03, 0.05, 0.20);
        vec3 base = mix(deep, u_accent, smoothstep(0.2, 0.9, sw));
        vec3 L = normalize(vec3(-0.5, 0.65, 0.75));
        float diff = max(dot(n, L), 0.0);
        float spec = pow(max(dot(reflect(-L, n), vec3(0.0, 0.0, 1.0)), 0.0), 24.0);
        float fres = pow(1.0 - n.z, 2.4);
        vec3 c = base * (0.30 + 0.95 * diff) + vec3(1.0) * spec * 0.85
               + mix(u_accent, vec3(0.7, 0.5, 1.0), 0.5) * fres * (1.0 + 1.8 * u_level + 0.6 * u_energy);
        float edge = smoothstep(1.0, 0.94, r);
        outc = vec4(c * edge, edge) + outc * (1.0 - edge);
    }
    gl_FragColor = outc;
}
)";

const char *kRibbonV = R"(
attribute vec2 a_pos;
attribute vec4 a_col;
varying vec4 v_col;
void main() { v_col = a_col; gl_Position = vec4(a_pos, 0.0, 1.0); }
)";
const char *kRibbonF = R"(
varying vec4 v_col;
void main() { gl_FragColor = v_col; }
)";

bool build(QOpenGLShaderProgram &p, const char *vs, const char *fs, const QList<QPair<int, const char *>> &attrs, QString *err) {
    if (!p.addShaderFromSourceCode(QOpenGLShader::Vertex, QByteArray(kPrelude) + vs) ||
        !p.addShaderFromSourceCode(QOpenGLShader::Fragment, QByteArray(kPrelude) + fs)) {
        *err = p.log();
        return false;
    }
    for (const auto &a : attrs) p.bindAttributeLocation(a.second, a.first);
    if (!p.link()) {
        *err = p.log();
        return false;
    }
    return true;
}

}  // namespace

GlStage::GlStage(QWidget *parent) : QOpenGLWidget(parent), m_target(kCols, 0.f), m_shown(kCols, 0.f) {
    setAttribute(Qt::WA_TransparentForMouseEvents);
    setAttribute(Qt::WA_NoSystemBackground);
    setTextureFormat(GL_RGBA8);
    m_timer = new QTimer(this);
    connect(m_timer, &QTimer::timeout, this, qOverload<>(&GlStage::update));
}

GlStage::~GlStage() {
    makeCurrent();
    m_quad.destroy();
    m_strip.destroy();
    doneCurrent();
}

void GlStage::setActivity(Activity a) {
    m_activity = a;
    retime();
}

void GlStage::retime() {
    if (!isVisible()) { m_timer->stop(); return; }
    m_timer->start(m_activity == Activity::Idle ? 33 : 16);
}

void GlStage::showEvent(QShowEvent *e) {
    QOpenGLWidget::showEvent(e);
    retime();
}

void GlStage::hideEvent(QHideEvent *e) {
    QOpenGLWidget::hideEvent(e);
    m_timer->stop();
}

void GlStage::pushLevel(qreal level01) {
    m_target.remove(0);
    m_target.append(float(std::clamp(level01, 0.0, 1.0)));
    m_lastLevelMs = m_clock.isValid() ? m_clock.elapsed() : 0;
}

void GlStage::initializeGL() {
    initializeOpenGLFunctions();
    QString err;
    bool ok = true;
    ok &= build(m_bg, kVertex, kBackdrop, {{0, "a_pos"}}, &err);
    if (ok) ok &= build(m_orb, kVertex, kOrb, {{0, "a_pos"}}, &err);
    if (ok) ok &= build(m_ribbon, kRibbonV, kRibbonF, {{0, "a_pos"}, {1, "a_col"}}, &err);
    if (!ok) {
        m_ok = false;
        if (!m_failedSent) {
            m_failedSent = true;
            const QString msg = "OpenGL shaders failed: " + err.left(300);
            QMetaObject::invokeMethod(this, [this, msg] { emit failed(msg); }, Qt::QueuedConnection);
        }
        return;
    }
    const float quad[8] = {0, 0, 1, 0, 0, 1, 1, 1};
    m_quad.create();
    m_quad.bind();
    m_quad.allocate(quad, sizeof(quad));
    m_quad.release();
    m_strip.create();
    m_strip.setUsagePattern(QOpenGLBuffer::StreamDraw);
    m_clock.start();
    m_ok = true;
}

void GlStage::resizeGL(int, int) {}

QRectF GlStage::anchorRect(const QWidget *w) const {
    if (!w || !w->isVisible() || !parentWidget()) return {};
    const QPoint tl = w->mapTo(parentWidget(), QPoint(0, 0)) - geometry().topLeft();
    return QRectF(tl, QSizeF(w->size()));
}

void GlStage::drawQuad(QOpenGLShaderProgram &prog, const QRectF &lr) {
    const float W = float(width()), H = float(height());
    const float x0 = float(lr.left()) / W * 2.f - 1.f, x1 = float(lr.right()) / W * 2.f - 1.f;
    const float y0 = 1.f - float(lr.bottom()) / H * 2.f, y1 = 1.f - float(lr.top()) / H * 2.f;
    prog.setUniformValue("u_r", x0, y0, x1, y1);
    m_quad.bind();
    prog.enableAttributeArray(0);
    prog.setAttributeBuffer(0, GL_FLOAT, 0, 2, 2 * sizeof(float));
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    prog.disableAttributeArray(0);
    m_quad.release();
}

void GlStage::drawRibbons(const QRectF &r, float t) {
    if (r.width() < 40 || r.height() < 20) return;
    const float W = float(width()), H = float(height());
    struct Layer { float scale, lift, alpha, cr, cg, cb; };
    const float ar = m_accent.redF(), ag = m_accent.greenF(), ab = m_accent.blueF();
    const Layer layers[3] = {
        {0.62f, -0.30f, 0.30f, 0.20f, 0.90f, 1.00f},                                   // far, cyan
        {0.80f, -0.15f, 0.50f, 0.62f, 0.35f, 1.00f},                                   // middle, violet
        {1.00f, 0.00f, 0.95f, 0.5f * ar + 0.5f, 0.5f * ag + 0.5f, 0.5f * ab + 0.5f},   // front, accent
    };
    m_ribbon.bind();
    m_strip.bind();
    m_ribbon.enableAttributeArray(0);
    m_ribbon.enableAttributeArray(1);
    for (int li = 0; li < 3; ++li) {
        const Layer &L = layers[li];
        QVector<Vtx> v;
        v.reserve(kCols * 2);
        const float cx = float(r.center().x()), halfW = float(r.width()) * 0.5f * (0.55f + 0.45f * L.scale);
        const float yc = float(r.center().y()) + L.lift * float(r.height());
        const float amp = float(r.height()) * 0.44f * L.scale;
        const float strength = std::min(1.0f, L.alpha * 1.6f * float(m_waveStrength));
        for (int i = 0; i < kCols; ++i) {
            const float u = float(i) / float(kCols - 1);
            const float env = std::pow(std::sin(float(M_PI) * u), 0.8f);
            const float breathe = 0.07f + 0.05f * std::sin(t * 1.6f + float(i) * 0.35f + float(li));
            const float lv = std::max(m_shown[(i + li * 5) % kCols], breathe);
            const float a = amp * env * (0.06f + 0.94f * lv);
            const float x = cx + (u * 2.f - 1.f) * halfW;
            const float nx = x / W * 2.f - 1.f;
            const float ny0 = 1.f - (yc - a) / H * 2.f, ny1 = 1.f - (yc + a * 0.55f) / H * 2.f;
            const float k = strength;
            v.append({nx, ny0, L.cr * k, L.cg * k, L.cb * k, k});                      // bright crest
            v.append({nx, ny1, L.cr * k * 0.10f, L.cg * k * 0.10f, L.cb * k * 0.10f, k * 0.10f});   // faded base
        }
        m_strip.allocate(v.constData(), int(v.size() * sizeof(Vtx)));
        m_ribbon.setAttributeBuffer(0, GL_FLOAT, 0, 2, sizeof(Vtx));
        m_ribbon.setAttributeBuffer(1, GL_FLOAT, 2 * sizeof(float), 4, sizeof(Vtx));
        glDrawArrays(GL_TRIANGLE_STRIP, 0, int(v.size()));
    }
    m_ribbon.disableAttributeArray(0);
    m_ribbon.disableAttributeArray(1);
    m_strip.release();
    m_ribbon.release();
}

void GlStage::paintGL() {
    glClearColor(0.f, 0.f, 0.f, 0.f);
    glClear(GL_COLOR_BUFFER_BIT);
    if (!m_ok) return;
    const float t = float(m_clock.elapsed()) / 1000.f;
    const qreal dpr = devicePixelRatioF();

    // smooth the inputs (levels decay when no fresh playback data arrives)
    const bool fresh = m_clock.elapsed() - m_lastLevelMs < 160;
    for (int i = 0; i < kCols; ++i) {
        if (!fresh) m_target[i] *= 0.90f;
        m_shown[i] += (m_target[i] - m_shown[i]) * 0.35f;
    }
    const float lv = m_shown[kCols - 1];
    m_level += (lv - m_level) * 0.25f;
    const float eTarget = m_activity == Activity::Idle ? 0.f : 1.f;
    m_energy += (eTarget - m_energy) * 0.06f;
    const QPoint cur = mapFromGlobal(QCursor::pos());
    const QPointF tgt = rect().contains(cur) ? QPointF(qreal(cur.x()) / width(), 1.0 - qreal(cur.y()) / height()) : QPointF(0.5, 0.5);
    m_mouse += (tgt - m_mouse) * 0.06;

    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

    m_bg.bind();
    m_bg.setUniformValue("u_res", float(width() * dpr), float(height() * dpr));
    m_bg.setUniformValue("u_time", t);
    m_bg.setUniformValue("u_level", m_level);
    m_bg.setUniformValue("u_energy", m_energy);
    m_bg.setUniformValue("u_radius", float(m_radius * dpr));
    m_bg.setUniformValue("u_mouse", float(m_mouse.x()), float(m_mouse.y()));
    m_bg.setUniformValue("u_accent", m_accent.redF(), m_accent.greenF(), m_accent.blueF());
    drawQuad(m_bg, QRectF(0, 0, width(), height()));
    m_bg.release();

    const QRectF orb = anchorRect(m_orbAnchor);
    if (!orb.isEmpty()) {
        const qreal side = qMax(orb.width(), orb.height()) * 2.5;
        QRectF q(0, 0, side, side);
        q.moveCenter(orb.center());
        m_orb.bind();
        m_orb.setUniformValue("u_time", t);
        m_orb.setUniformValue("u_level", m_level);
        m_orb.setUniformValue("u_energy", m_energy);
        m_orb.setUniformValue("u_accent", m_accent.redF(), m_accent.greenF(), m_accent.blueF());
        drawQuad(m_orb, q);
        m_orb.release();
    }

    glBlendFunc(GL_ONE, GL_ONE);                    // ribbons add light
    drawRibbons(anchorRect(m_waveAnchor), t);
    glDisable(GL_BLEND);
}
