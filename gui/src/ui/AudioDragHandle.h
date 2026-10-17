#pragma once
// "Drag audio" pill for the title bar: grab it and drop the generated audio straight into a video editor, Explorer or any
// app that takes files - no save dialog. A plain click copies the file to the clipboard instead. A small WAV / MP3 chip on
// the pill switches the format. The file itself comes from a provider callback (a temporary copy owned by MainWindow),
// so this widget only deals with the gesture and the look.
#include <QWidget>
#include <functional>

class AudioDragHandle : public QWidget {
    Q_OBJECT
public:
    enum class State { NoAudio, Preparing, Ready };

    explicit AudioDragHandle(QWidget *parent = nullptr);
    void setState(State s);
    State state() const { return m_state; }
    QString fileForDrag() const { return m_provider ? m_provider() : QString(); }   // what a drag would hand over now
    void setFormat(const QString &fmt);                    // "wav" | "mp3"
    QString format() const { return m_fmt; }
    // Returns the file to hand over, or an empty string if it cannot be produced right now.
    void setProvider(std::function<QString()> p) { m_provider = std::move(p); }
    QSize sizeHint() const override { return QSize(176, 28); }
    QSize minimumSizeHint() const override { return QSize(150, 26); }

signals:
    void formatChanged(const QString &fmt);
    void copied(const QString &file);                      // click: file was put on the clipboard
    void dragged(const QString &file, bool accepted);      // drag finished

protected:
    void paintEvent(QPaintEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;
    void enterEvent(QEnterEvent *) override;
    void leaveEvent(QEvent *) override;

private:
    QRectF chipRect() const;
    QPixmap dragPixmap(const QString &file) const;
    State m_state = State::NoAudio;
    QString m_fmt = "wav";
    std::function<QString()> m_provider;
    QPoint m_press;
    bool m_down = false, m_onChip = false, m_hover = false, m_dragging = false;
};
