#pragma once
// Horizontally scrolling strip of voice "cards" that mirrors a QComboBox (the combo stays the source of truth, so all
// existing logic and tests keep working): the chips are rebuilt whenever the combo's items change, clicking a chip
// selects that item, and the selected chip scrolls into view. Mouse wheel and the arrow buttons scroll sideways.
#include <QAbstractButton>
#include <QWidget>

class QComboBox;
class QHBoxLayout;
class QScrollArea;
class QToolButton;

class VoiceChip : public QAbstractButton {
    Q_OBJECT
public:
    explicit VoiceChip(const QString &name, QWidget *parent = nullptr);
    QSize sizeHint() const override { return QSize(108, 60); }
    QSize minimumSizeHint() const override { return sizeHint(); }

protected:
    void paintEvent(QPaintEvent *) override;
    void enterEvent(QEnterEvent *) override;
    void leaveEvent(QEvent *) override;

private:
    bool m_hover = false;
};

class VoiceCarousel : public QWidget {
    Q_OBJECT
public:
    explicit VoiceCarousel(QComboBox *source, QWidget *parent = nullptr);
    int chipCount() const { return m_chips.size(); }

protected:
    bool eventFilter(QObject *, QEvent *) override;

private:
    void rebuild();
    void sync();
    void scrollBy(int dx);
    QComboBox *m_src;
    QScrollArea *m_scroll;
    QWidget *m_inner;
    QHBoxLayout *m_row;
    QToolButton *m_left, *m_right;
    QList<VoiceChip *> m_chips;
    bool m_rebuilding = false;
};
