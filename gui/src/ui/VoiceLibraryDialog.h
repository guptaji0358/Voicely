#pragma once
// Voice library: every voice as a card in a searchable grid (All / Girls / Boys), each with its own play button for the
// stored audio sample. Choosing a voice just reports its name; MainWindow's voice combo stays the source of truth.
#include <QDialog>
#include <QList>
#include <QString>

class QAudioOutput;
class QGridLayout;
class QLabel;
class QLineEdit;
class QMediaPlayer;
class QPushButton;
class QWidget;

struct VoiceEntry {
    QString name, display, gender, tagline, sample;   // gender: "female" | "male" | ""
};

class VoiceLibraryDialog : public QDialog {
    Q_OBJECT
public:
    VoiceLibraryDialog(const QList<VoiceEntry> &voices, const QString &current, QWidget *parent = nullptr);
    QString selectedName() const { return m_selected; }
    int cardCount() const { return m_cards.size(); }

private:
    struct Card {
        VoiceEntry v;
        QWidget *w = nullptr;
        QPushButton *play = nullptr;
    };
    void relayout();
    void select(const QString &name);
    void togglePlay(const QString &name);
    void stopPlayback();

    QList<VoiceEntry> m_voices;
    QList<Card> m_cards;
    QString m_selected, m_playing;
    QString m_filter;            // "", "female", "male"
    QLineEdit *m_search;
    QGridLayout *m_grid;
    QLabel *m_count, *m_empty;
    QLabel *m_headers[3] = {};
    QPushButton *m_use;
    QMediaPlayer *m_player;
    QAudioOutput *m_out;
};
