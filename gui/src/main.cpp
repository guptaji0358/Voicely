#include "MainWindow.h"

#include <QApplication>

int main(int argc, char *argv[]) {
    QApplication app(argc, argv);
    QApplication::setApplicationName("TTS Studio");
    QApplication::setOrganizationName("TTSStudio");
    MainWindow w;
    w.show();
    return app.exec();
}
