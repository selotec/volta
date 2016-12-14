# -*- coding: utf-8 -*-

import time
import logging
import sqlite3
import usb
import subprocess
import glob
import json
import argparse

from pkg_resources import resource_string, resource_filename


logger = logging.getLogger(__name__)


def EventPoller(event):
    while True:
        rc = event()
        if rc is not None:
            return rc
        else:
            time.sleep(1)

class VoltaWorker(object):
    def __init__(self):
        self.volta = None
        self.test_duration = None
        self.worker = None
        self.volta_idVendor = 0x1a86 # CH341 idVendor=0x1a86, idProduct=0x7523
        self.volta_idProduct = 0x7523


    def isUsbConnected(self):
        logger.info("Подключите коробочку в USB...")
        device = usb.core.find(idVendor=self.volta_idVendor, idProduct=self.volta_idProduct)
        if device:

            logger.info('Найдена коробочка')
            return device

    def getTestDuration(self):
        logger.info('Введите длительность теста в секундах (сколько секунд коробочка будет записывать данные тока)')
        try:
            duration = int(raw_input())
        except ValueError:
            logger.error('Введите только число, длительность теста в секундах', exc_info=True)
        else:
            logger.info('Принято. Длительность теста будет %s секунд', duration)
            self.test_duration = duration
            return True

    def setTestDuration(self, duration):
        self.test_duration = duration
        return True

    def startTest(self):
        ports = glob.glob('/dev/cu.[A-Za-z]*')
        device = [port for port in ports if 'Bluetooth' not in port][0]
        self.worker = subprocess.Popen(
            'volta-grab --device {device} -s {duration}'.format(duration=self.test_duration, device=device),
            shell=True
        )
        logger.info('Тест уже начался. Не забудьте помигать на телефоне фонариком!')

    def isTestFinished(self):
        if self.worker:
            if self.worker.poll() is not None:
                rc = self.worker.poll()
                logging.info('Grabber finsihed')
                return rc
        else:
            raise('Worker died?')

    def upload(self, output, events):
        logger.info('Считаем кросс-корреляцию и загружаем логи в Лунапарк')
        upload = subprocess.Popen('volta-uploader -f {output} -e {events}'.format(output=output, events=events),
                                  stdout=subprocess.PIPE, shell=True
                                  )

        rc = upload.wait()
        jobid = upload.stdout.read()
        logging.info('Upload завершился: %s', rc)
        if rc is not None:
            return jobid

class PhoneWorker(object):
    def __init__(self):
        self.db = sqlite3.connect(resource_filename("volta.analysis", 'usb_list.db')).cursor()
        self.known_phones = [
            u'SAMSUNG_Android', u'Android', u'Nexus 5X', u'FS511',
            u'iPhone',
        ]
        self.device_serial = None
        self.device_name = None
        self.android_version = None
        self.android_api_version = None

    def isPhoneConnected(self):
        logger.info("Подключите телефон в USB...")
        # ищем все подключенные известные нам телефоны по атрибуту product
        logger.debug('Found products: %s', [device.product for device in usb.core.find(find_all=1)])
        phones = [device for device in usb.core.find(find_all=1) if device.product in self.known_phones]
        if len(phones) == 1 :
            # id'шники преобразовываем в hex, соблюдая формат
            self.db.execute(
                'SELECT name FROM devices WHERE manufacturer_id="{man_id}" AND id="{device_id}"'.format(
                    man_id=format(phones[0].idVendor, '04x'),
                    device_id=format(phones[0].idProduct, '04x'),
                )
            )
            # известного нам девайса может не быть в базе vendor->usb устройств
            try:
                device_name = self.db.fetchone()[0].encode('utf-8')
            except:
                device_name = 'Unknown device'
            logger.info('Найден телефон: %s. id: %s', device_name, phones[0].serial_number.encode('utf-8'))
            self.device_serial = phones[0].serial_number
            self.device_name = device_name
            return self.device_serial
        elif len(phones) > 1:
            logger.info('Найдено более 1 телефона! Отключите те, что не будут участвовать в измерениях.')
        return

    def isPhoneDisconnected(self):
        logger.info("Отключите телефон от USB...")
        devices = usb.core.find(find_all=1)
        phones = []
        for device in devices:
            if not device.product in self.known_phones:
                continue
            else:
                phones.append(device)
        if len(phones) >= 1 :
            q = 'SELECT name FROM devices WHERE manufacturer_id="{man_id}" AND id="{device_id}"'.format(
                man_id=format(phones[0].idVendor, '04x'),
                device_id=format(phones[0].idProduct, '04x'),
            )
            self.db.execute(q)
            try:
                device_name = self.db.fetchone()[0].encode('utf-8')
            except:
                device_name = 'Unknown device'
            logging.info('Найден телефон: %s. id: %s', device_name, phones[0].serial_number.encode('utf-8'))
            return
        elif len(phones) == 0:
            logging.info('Телефон отключён. Запускаем тест.')
            return True

    def clearLogcat(self):
        logger.info('Чистим logcat на телефоне')
        logcat_clear = subprocess.Popen('adb logcat -c', shell=True)
        rc = logcat_clear.wait()
        logging.info('Logcat clear завершился с RC: %s', rc)
        if rc is not None:
            return rc

    def dumpLogcatEvents(self, output='./events.log'):
        logger.info('Забираем лог эвентов с телефона и складываем в %s', output)
        logcat_dump = subprocess.Popen('adb logcat -d > {output}'.format(output=output), shell=True)
        rc = logcat_dump.wait()
        logging.info('Logcat dump завершился с RC: %s', rc)
        if rc is not None:
            return rc

    def getInfoAboutDevice(self):
        logger.info('Получаем информацию об устройстве')
        adb = subprocess.Popen('adb shell getprop ro.build.version.release', stdout=subprocess.PIPE, shell=True)
        adb.wait()
        self.android_version = adb.communicate()[0].strip('\n')
        adb2 = subprocess.Popen('adb shell getprop ro.build.version.sdk ', stdout=subprocess.PIPE, shell=True)
        self.android_api_version = adb2.communicate()[0].strip('\n')
        adb2.wait()

        #dump to file
        res = {
            'android_version': self.android_version,
            'android_api_version': self.android_api_version,
            'device_name': self.device_name,
            'device_id': self.device_serial
        }
        with open('meta.json', 'w') as fname:
            fname.write(json.dumps(res))
        return True

def main():
    parser = argparse.ArgumentParser(
        description='wizard for volta.')
    parser.add_argument(
        '-d', '--debug',
        help='enable debug logging',
        action='store_true')
    args = parser.parse_args()
    logging.basicConfig(
        level="DEBUG" if args.debug else "INFO",    
        format='%(asctime)s [%(levelname)s] [WIZARD] %(filename)s:%(lineno)d %(message)s'
    )
    logger.info("Volta wizard started")
    volta = VoltaWorker()
    phone = PhoneWorker()
    # 1 - подключение коробки
    volta.device = EventPoller(volta.isUsbConnected)

    # 2 - подключение телефона
    EventPoller(phone.isPhoneConnected)
    # 3 - установка apk
    # TODO
    # 4 - параметризация теста
    EventPoller(volta.getTestDuration)
    # pre5 - сброс logcat
    EventPoller(phone.clearLogcat)
    # 5 - отключение телефона
    EventPoller(phone.isPhoneDisconnected)
    # 6 - запуск теста, мигание фонариком
    volta.startTest()
    EventPoller(volta.isTestFinished)
    # 7 - подключение телефона
    EventPoller(phone.isPhoneConnected)
    EventPoller(phone.dumpLogcatEvents)
    EventPoller(phone.getInfoAboutDevice)
    # 8 - заливка логов
    volta.upload('./output.bin', './events.log')

    logger.info('Volta wizard finished')




if __name__ == "__main__":
    main()