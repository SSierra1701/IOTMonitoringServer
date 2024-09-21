from argparse import ArgumentError
import ssl
from django.db.models import Avg
from datetime import timedelta, datetime
from receiver.models import Data, Measurement
import paho.mqtt.client as mqtt
import schedule
import time
from django.conf import settings

#client = mqtt.Client(settings.MQTT_USER_PUB)
client = mqtt.Client(client_id=settings.MQTT_USER_PUB, protocol=mqtt.MQTTv311, transport="tcp")

def analyze_data():
    # Consulta todos los datos de la última hora, los agrupa por estación y variable
    # Compara el promedio con los valores límite que están en la base de datos para esa variable.
    # Si el promedio se excede de los límites, se envia un mensaje de alerta.

    print("Calculando alertas...")

    data = Data.objects.filter(
        base_time__gte=datetime.now() - timedelta(hours=1))
    aggregation = data.annotate(check_value=Avg('avg_value')) \
        .select_related('station', 'measurement') \
        .select_related('station__user', 'station__location') \
        .select_related('station__location__city', 'station__location__state',
                        'station__location__country') \
        .values('check_value', 'station__user__username',
                'measurement__name',
                'measurement__max_value',
                'measurement__min_value',
                'station__location__city__name',
                'station__location__state__name',
                'station__location__country__name')
    alerts = 0
    for item in aggregation:
        alert = False

        variable = item["measurement__name"]
        max_value = item["measurement__max_value"] or 0
        min_value = item["measurement__min_value"] or 0

        country = item['station__location__country__name']
        state = item['station__location__state__name']
        city = item['station__location__city__name']
        user = item['station__user__username']

        if item["check_value"] > max_value or item["check_value"] < min_value:
            alert = True

        if alert:
            message = "ALERT {} {} {}".format(variable, min_value, max_value)
            topic = '{}/{}/{}/{}/in'.format(country, state, city, user)
            print(datetime.now(), "Sending alert to {} {}".format(topic, variable))
            client.publish(topic, message)
            alerts += 1

    print(len(aggregation), "dispositivos revisados")
    print(alerts, "alertas enviadas")


def analyze_percentage_change_by_seconds():
    # Consulta los datos de los últimos 30 segundos y los compara con los 30 segundos anteriores
    print("Calculando alertas por cambio porcentual en segundos...")

    now = datetime.now()
    sixty_seconds_ago = now - timedelta(seconds=60)
    thirty_seconds_ago = now - timedelta(seconds=30)

    # Consulta datos del último período (últimos 30 segundos)
    current_data = Data.objects.filter(base_time__gte=thirty_seconds_ago)
    # Consulta datos del período anterior (30 a 60 segundos atrás)
    previous_data = Data.objects.filter(base_time__gte=sixty_seconds_ago, base_time__lt=thirty_seconds_ago)

    # Agrupa por estación y variable
    current_aggregation = current_data.annotate(current_avg=Avg('avg_value')) \
        .select_related('station', 'measurement') \
        .select_related('station__user', 'station__location') \
        .values('current_avg', 'station__user__username', 'measurement__name', 'station__location__city__name')

    previous_aggregation = previous_data.annotate(previous_avg=Avg('avg_value')) \
        .select_related('station', 'measurement') \
        .select_related('station__user', 'station__location') \
        .values('previous_avg', 'station__user__username', 'measurement__name', 'station__location__city__name')

    # Combina ambas consultas por estación y variable
    alerts = 0
    for current, previous in zip(current_aggregation, previous_aggregation):
        # Verifica si son del mismo usuario y misma medición
        if current['measurement__name'] == previous['measurement__name'] and current['station__user__username'] == previous['station__user__username']:
            # Calcula el cambio porcentual
            if previous['previous_avg'] != 0:
                percentage_change = ((current['current_avg'] - previous['previous_avg']) / previous['previous_avg']) * 100
            else:
                percentage_change = 0

            # Umbral de cambio porcentual
            threshold = 2
            print(percentage_change, threshold)
            if abs(percentage_change) > threshold:
                variable = current['measurement__name']
                city = current['station__location__city__name']
                user = current['station__user__username']
                topic = '{}/{}/in'.format(city, user)
                message = "ALERT: {} cambió {}% en los últimos 30 segundos".format(variable, round(percentage_change, 2))

                print(now, "Enviando alerta de cambio porcentual a {} {}".format(topic, variable))
                client.publish(topic, message)
                alerts += 1

    print("Alertas enviadas:", alerts)



def on_connect(client, userdata, flags, rc):
    '''
    Función que se ejecuta cuando se conecta al bróker.
    '''
    print("Conectando al broker MQTT...", mqtt.connack_string(rc))


def on_disconnect(client: mqtt.Client, userdata, rc):
    '''
    Función que se ejecuta cuando se desconecta del broker.
    Intenta reconectar al bróker.
    '''
    print("Desconectado con mensaje:" + str(mqtt.connack_string(rc)))
    print("Reconectando...")
    client.reconnect()


def setup_mqtt():
    '''
    Configura el cliente MQTT para conectarse al broker.
    '''

    print("Iniciando cliente MQTT...", settings.MQTT_HOST, settings.MQTT_PORT)
    global client
    try:
        client = mqtt.Client(settings.MQTT_USER_PUB)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect

        if settings.MQTT_USE_TLS:
            client.tls_set(ca_certs=settings.CA_CRT_PATH,
                           tls_version=ssl.PROTOCOL_TLSv1_2, cert_reqs=ssl.CERT_NONE)

        client.username_pw_set(settings.MQTT_USER_PUB,
                               settings.MQTT_PASSWORD_PUB)
        client.connect(settings.MQTT_HOST, settings.MQTT_PORT)

    except Exception as e:
        print('Ocurrió un error al conectar con el bróker MQTT:', e)


def start_cron():
    '''
    Inicia el cron que se encarga de ejecutar la función analyze_data cada 5 minutos.
    '''
    print("Iniciando cron...")
    schedule.every(5).minutes.do(analyze_data)
    print("Servicio de control iniciado")
    while 1:
        schedule.run_pending()
        time.sleep(1)
