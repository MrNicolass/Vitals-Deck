# TURZX 3.5 Monitor

Monitor de sistema para a mini tela **TURZX / Turing Smart Screen 3.5"** (320x480, USB CDC-ACM, `1a86:5722`), sem o software proprietário do fabricante. Roda como serviço *systemd* em Linux, coleta CPU, RAM, Swap, GPUs NVIDIA e temperatura, e acumula o consumo de energia.

## Como a tela funciona

A tela não é um monitor de vídeo: o microcontrolador CH552T recebe comandos de 6 bytes e o *framebuffer* em RGB565 pela serial virtual e repassa ao painel. A taxa efetiva medida é de ~140 KB/s, então um frame completo de 320x480 custa ~1,9 s. Por isso o programa atualiza apenas as células que mudaram e reenvia o frame inteiro a cada `full_refresh_interval`.

Detalhes do protocolo Rev A:

- Comandos: `CLEAR` (102), `SCREEN_OFF`/`SCREEN_ON` (108/109), `SET_BRIGHTNESS` (110), `SET_ORIENTATION` (121) e `DISPLAY_BITMAP` (197);
- Cada comando usa 6 bytes, com as coordenadas de 10 bits empacotadas em 5 bytes;
- `SET_ORIENTATION` é o único comando de 16 bytes (6 de cabeçalho, orientação + 100, largura e altura em *big-endian* e 5 bytes de preenchimento);
- O brilho é absoluto (0 = mais claro, 255 = mais escuro) e `CLEAR` exige orientação retrato antes do envio;
- Os pixels vão em RGB565 *little-endian*, em blocos de `largura * 8` bytes.

## Requisitos

- Linux com `cdc_acm` e RAPL (`intel_rapl`) disponíveis;
- Python 3.12;
- Driver NVIDIA com `nvidia-smi` funcional (opcional, para os *gauges* de GPU);
- Acesso à serial pelo grupo `dialout`;
- Leitura em `energy_uj` para contabilizar a energia da CPU.

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Para instalar como serviço (cria unidade *systemd*, regra *udev*, adiciona o usuário ao grupo `dialout`, habilita e inicia):

```bash
sudo ./install.sh
```

O `config.yaml` precisa existir antes do `install.sh`, e o serviço roda com o usuário dono do diretório do projeto (pode ser sobrescrito com `SERVICE_USER=...`).

### Execução manual

```bash
.venv/bin/python -m turzx_monitor --config config.yaml
```

### Preview offline

Gera um PNG do layout sem precisar da tela conectada:

```bash
.venv/bin/python -m turzx_monitor --preview /tmp/turzx-preview.png
```

## Configuração

O carregamento é estrito: qualquer chave desconhecida impede a inicialização e todas as faixas são validadas.

| Seção | Chave | Padrão | Descrição |
|---|---|---|---|
| `display` | `port` | `/dev/serial/by-id/usb-Turing_UsbMonitor_USB35INCHIPSV2-if00` | Serial da tela |
| `display` | `baudrate` | `115200` | Ignorado pelo CDC-ACM, mantido por compatibilidade |
| `display` | `orientation` | `portrait` | Única orientação aceita |
| `display` | `brightness` | `0` | 0 = mais claro, 255 = mais escuro |
| `display` | `full_refresh_interval` | `300` | Segundos entre frames completos |
| `display` | `reconnect_delay` | `5` | Segundos entre tentativas de reconexão |
| `monitor` | `interval` | `2.0` | Segundos entre atualizações de células |
| `monitor` | `gauges` | `cpu, ram, swap, gpu0, gpu1` | Células exibidas |
| `monitor` | `gpus` | `0, 1` | Índices passados ao `nvidia-smi` |
| `energy` | `state_file` | `/var/lib/turzx-monitor/energy.json` | Estado persistido de energia |
| `energy` | `sample_interval` | `1.0` | Segundos entre amostras de energia |
| `energy` | `rapl_path` | `/sys/class/powercap/intel-rapl:0` | Domínio RAPL da CPU |
| `logging` | `level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` ou `CRITICAL` |
| `logging` | `file` | `null` | Arquivo de log; `null` usa *stderr* |

## Energia

A gravação é feita pelo `EnergyAccumulator` (`turzx_monitor/energy.py`), acionado a cada `energy.sample_interval`:

- **CPU**: delta de `energy_uj` do RAPL; recuos maiores que metade de `max_energy_range_uj` são tratados como *wrap* do contador e recuos menores, como reinício (delta descartado);
- **GPUs**: integração de `power.draw × delta_t`, ignorada quando o intervalo passa de 60 s;
- **Formato**: `energy.json` versão 1 com `day`, `month`, `day_joules`, `month_joules`, `rapl_uj` e `updated`;
- **Atomicidade**: escreve em `energy.json.tmp` e faz `os.replace`, sem deixar arquivo truncado;
- **Continuidade**: no *start*, relê `rapl_uj` para continuar o delta exato após reinício; estado ausente ou inválido recomeça do zero com aviso em log;
- **Rollover**: zera `day_joules` na virada do dia e `month_joules` na virada do mês, em horário local;
- **Exibição**: a tela converte joules em Wh (`/ 3600`) e mostra dia e mês.

O total exibido é **CPU+GPU**; não inclui RAM, discos nem perdas da fonte.

## Testes

A suíte usa apenas a biblioteca padrão:

```bash
.venv/bin/python -m unittest discover -s tests
```

## Estrutura

| Arquivo | Função |
|---|---|
| `turzx_monitor/config.py` | Carregamento e validação estrita do `config.yaml` |
| `turzx_monitor/protocol.py` | Protocolo Rev A: *handshake*, orientação, brilho, limpeza e *bitmap* parcial |
| `turzx_monitor/metrics.py` | Coleta CPU, RAM, Swap, GPUs e temperatura sem `psutil` |
| `turzx_monitor/energy.py` | Acumulador de energia com persistência atômica e *rollover* |
| `turzx_monitor/render.py` | Layout 320x480, *gauges* de 270° e conversão RGB565 |
| `turzx_monitor/app.py` | *Loop* principal, *diff* de células, reconexão e sinais |
| `turzx_monitor/preview.py`, `__main__.py` | *Preview* PNG e CLI |
| `deploy/`, `install.sh` | Unidade *systemd*, regra *udev* e instalação idempotente |
| `tests/` | Testes de protocolo, métricas, energia, renderização e *loop* |

## Diagnóstico

- `Permission denied` na serial: conferir a regra *udev*, o grupo `dialout` e `ls -l /dev/ttyACM0`;
- Energia de CPU zerada: conferir a permissão de `energy_uj` e se `energy.rapl_path` existe;
- *Gauges* de GPU em `--%`: testar `nvidia-smi` com o usuário do serviço;
- Tela congelada ou com artefatos: reiniciar o serviço para forçar frame completo;
- Nada é enviado: confirmar que só uma instância está rodando e que a tela está no *link* configurado.

## Referências

- [turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) — implementação *open-source* do protocolo;
- [TURZX 3.5" Smart Screen / A Reverse Engineering Attempt Gone Wrong](https://www.harukadev.com/blog/turzx-35-smart-screen-reverse-engineering) — *teardown* e aviso sobre a fragilidade do LCD.

## Licença

MIT, veja [LICENSE](LICENSE).
