#pragma once
// OpenGL "stage" that sits behind every widget of the window and draws the 3D look:
//   * animated backdrop    aurora, receding perspective grid floor and parallax particles (mouse-reactive)
//   * orb                  a lit, swirling sphere behind the logo; pulses with the voice level / while generating
//   * waveform ribbons     three depth layers of a glowing ribbon driven by real playback levels
// Everything is drawn in one context. The widget is optional: if OpenGL or the shaders are unavailable it emits
// failed() and the owner falls back to the flat (QPainter) look.
#include <QColor>
#include <QElapsedTimer>
#include <QOpenGLBuffer>
#include <QOpenGLFunctions>
#include <QOpenGLShaderProgram>
#include <QOpenGLWidget>
#include <QPointer>
#include <QVector>

class QTimer;

class GlStage : public QOpenGLWidget, protected QOpenGLFunctions {
    Q_OBJECT
public:
    enum class Activity { Idle, Busy, Playing };

    explicit GlStage(QWidget *parent = nullptr);
    ~GlStage() override;

    void setCornerRadius(qreal px) { m_radius = px; }
    void setOrbAnchor(QWidget *w) { m_orbAnchor = w; }
    void setWaveAnchor(QWidget *w) { m_waveAnchor = w; }
    void setWaveStrength(qreal s) { m_waveStrength = s; }      // 0..1 how bright the ribbons are (dim when no audio)
    void setAccent(const QColor &c) { m_accent = c; }
    void setActivity(Activity a);
    void pushLevel(qreal level01);                               // real playback level, 0..1
    bool ok() const { return m_ok; }

signals:
    void failed(const QString &reason);

protected:
    void initializeGL() override;
    void resizeGL(int w, int h) override;
    void paintGL() override;
    void showEvent(QShowEvent *) override;
    void hideEvent(QHideEvent *) override;

private:
    struct Vtx { float x, y, r, g, b, a; };
    QRectF anchorRect(const QWidget *w) const;                   // logical px, y down, in this widget
    void drawQuad(QOpenGLShaderProgram &prog, const QRectF &logicalRect);
    void drawRibbons(const QRectF &r, float t);
    void retime();

    QOpenGLShaderProgram m_bg, m_orb, m_ribbon;
    QOpenGLBuffer m_quad{QOpenGLBuffer::VertexBuffer}, m_strip{QOpenGLBuffer::VertexBuffer};
    QTimer *m_timer = nullptr;
    QElapsedTimer m_clock;
    QPointer<QWidget> m_orbAnchor, m_waveAnchor;
    QColor m_accent{0x3d, 0x7e, 0xff};
    qreal m_radius = 12, m_waveStrength = 0.35;
    bool m_ok = false, m_failedSent = false;
    Activity m_activity = Activity::Idle;
    float m_level = 0, m_energy = 0;                             // smoothed level, smoothed activity
    QPointF m_mouse{0.5, 0.5};
    static constexpr int kCols = 72;
    QVector<float> m_target, m_shown;
    qint64 m_lastLevelMs = 0;
};
