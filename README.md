# SQLBridge — SQL Server → MySQL, PostgreSQL ou Oracle

Aplicação web local em Python 3.11+ com Flask, SQLAlchemy, pymssql (FreeTDS), PyMySQL, psycopg e python-oracledb. Preparada para catálogo do **SQL Server 2008 (10.0) / 2008 R2 (10.50)** e destinos **MySQL 8**, **PostgreSQL 12+** ou **Oracle 12.1+** (driver thin). Não usa ODBC nem precisa publicar um site. Selecione o banco no formulário de destino. A compatibilidade com seu servidor deve ser confirmada com uma migração de teste: não foi possível acessar bancos reais neste ambiente.

## Instalação no Windows

Instale Python 3.11 ou 3.12 de 64 bits. No PowerShell, dentro da pasta do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Abra **http://127.0.0.1:8000** e mantenha o terminal aberto. Para encerrar, Ctrl+C. Não é necessário ativar o ambiente virtual. Depois de instalar, você também pode usar `iniciar.bat`.

## Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Os pacotes normalmente oferecem wheels. Caso não exista wheel para seu sistema/Python, a compilação de pymssql exige FreeTDS e ferramentas de desenvolvimento. Prefira Python 3.11/3.12 de 64 bits.

## Preparação dos bancos

- SQL Server: habilite TCP/IP no SQL Server Configuration Manager, confirme a porta real da instância e use autenticação SQL Server. Instâncias nomeadas podem usar uma porta diferente de 1433: informe o host e a porta separados, sem `\\INSTANCIA` no campo servidor.
- Conta de origem: SELECT nas tabelas escolhidas e VIEW DEFINITION para refletir objetos e ler definições. Visibilidade de catálogo depende das permissões. Objetos criptografados retornam definição nula. A aplicação só lê a origem.
- MySQL: crie previamente um banco **exclusivo para migração** (preferencialmente vazio). Use uma conta com CREATE, INSERT, SELECT, ALTER, INDEX e REFERENCES nesse banco. Exemplo de criação pelo administrador:

```sql
CREATE DATABASE migracao CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
```

- Se os bancos estiverem em outra máquina, libere a porta no firewall e confirme o acesso a partir da máquina que executa o Python.
- SQL Server 2008: configure TLS 1.2 com os patches correspondentes quando possível. A conexão usa TDS 7.3 e exige criptografia por padrão. Se um servidor legado em rede confiável não negociar TLS, a opção de criptografia pode ser desmarcada conscientemente; nesse caso, os dados trafegam sem criptografia. Windows Authentication/SSO não está incluído nesta versão.
- Para MySQL remoto, marque TLS. O certificado deve ser confiável para o Python e corresponder ao nome do servidor. Sem essa opção, a aplicação não exige TLS no MySQL.

## Fluxo

1. Teste e conecte a origem e o destino. As senhas são removidas dos campos após uma conexão bem-sucedida, mas os engines mantêm as credenciais na memória.
2. Clique em **Ler catálogo** e selecione as tabelas.
3. Gere o plano. Revise tipos, nomes, DDL, bloqueios e avisos; baixe o ZIP com o plano e as definições originais.
4. Pause as escritas na origem. Não há snapshot global, CDC nem sincronização incremental: mudanças concorrentes podem produzir uma cópia inconsistente entre tabelas.
5. Digite **MIGRAR** e execute. O progresso informa linhas confirmadas e linhas da transação em andamento; o relatório confirma linhas efetivamente gravadas por tabela.
6. Baixe o relatório, valide contagens, valores e regras de negócio. A contagem do destino é comparada às linhas copiadas, sem checksum de conteúdo. Depois adapte e valide os objetos manuais antes de direcionar sua aplicação ao MySQL.

## Escopo automático

- Descoberta de tabelas, colunas, PKs, índices, UNIQUEs, FKs e CHECKs.
- Conversão dos tipos comuns; bloqueio de tipos que exigem decisão manual, como `datetimeoffset`, tipos espaciais e tipos desconhecidos.
- Criação das tabelas e PKs/UNIQUEs, cópia em lotes configuráveis (1 a 10.000 linhas), criação de índices comuns e FKs entre as tabelas selecionadas depois da carga.
- Preservação dos valores originais das identidades, sem recriar o gerador IDENTITY/AUTO_INCREMENT.
- Nome de destino `schema__tabela`. Nomes de tabela/índice acima de 64 caracteres são abreviados com hash; colisões entre nomes de tabelas são bloqueadas. Nomes de coluna acima de 64 caracteres bloqueiam o plano.
- `rowversion/timestamp` do SQL Server torna-se binário de 8 bytes; não vira data/hora. `money` torna-se NUMERIC(19,4), `uniqueidentifier` torna-se VARCHAR(36), tipos de texto grandes tornam-se LONGTEXT.
- Exportação das definições originais de views, procedures, funções e triggers em JSON. Sequences e synonyms são inventariados, mas suas propriedades específicas precisam ser extraídas/adaptadas separadamente.

## Adaptação manual e limites

Views, procedures, funções e triggers **não são convertidos nem executados automaticamente**. T-SQL, funções nativas, tratamento de erros, SQL dinâmico, tabelas temporárias e transações precisam de reescrita para MySQL. Esta versão entrega o inventário e as definições disponíveis para essa etapa.

Defaults, IDENTITY/AUTO_INCREMENT, expressões de colunas calculadas e CHECKs não são recriados; os valores das colunas são copiados. Índices filtrados, com INCLUDE ou columnstore exigem revisão. FKs para tabelas fora da seleção são omitidas e sinalizadas. FKs com SET DEFAULT bloqueiam o plano. Triggers não são ativadas durante a carga porque não são criadas.

Revise collation, comparação com espaços, acentos, maiúsculas e minúsculas, regras de UNIQUE com NULL, comprimento de índices, limite de linha do InnoDB, chaves sobre tipos TEXT/BLOB e diferenças de precisão temporal (MySQL até 6 casas; SQL Server até 7). Algumas incompatibilidades podem ser detectadas apenas na execução pelo MySQL; o plano não certifica equivalência de comportamento. A conexão SQL Server 2008 será validada no ambiente real.

## Controle da execução e retorno do MySQL

Antes de executar, escolha a política para tabelas existentes:

- **Interromper antes de criar objetos**: padrão conservador. Se houver conflito de nome com tabela ou view, nenhuma tabela nova é criada.
- **Pular tabela existente e continuar**: preserva toda a estrutura e os dados da tabela existente. Não insere registros nela, não recria seus índices/FKs e não a considera validada. Isso permite migrar as demais tabelas, mas não retoma nem completa uma carga parcial.

A inspeção do destino aparece no plano quando ele está conectado. A execução verifica novamente o catálogo, pois o destino pode mudar entre o plano e a carga.

A opção **Continuar com os demais objetos após uma falha**, habilitada na interface, isola falhas de criação, carga, índices e FKs. Desmarque para interromper no primeiro erro. Falhas globais de catálogo/conexão inicial ou incompatibilidades bloqueantes do plano ainda impedem a execução.

O acompanhamento lista cada operação como **criado e verificado**, **carga confirmada**, **pulado** ou **falhou**. Filtre para localizar falhas e pendências. Para DDL bem-sucedido, a aplicação confirma COMMIT, verifica o objeto no catálogo e, no MySQL, mostra o `SHOW CREATE TABLE` retornado pelo servidor após aquela operação. Não é um código numérico de sucesso: DDL MySQL normalmente não retorna linhas. Os avisos de `SHOW WARNINGS` também são registrados.

Erros incluem etapa, objeto, classe, código do servidor quando disponível, referência e orientação. Exemplos: 1071 (índice longo), 1170 (índice sobre TEXT/BLOB sem prefixo), 1118 (limite de linha), 1062 (duplicidade), 1452 (registros órfãos), 3780 (tipos de FK incompatíveis), erros de permissão e de conexão. Essas orientações não substituem a revisão do esquema. Valores dos registros presentes em mensagens de erro da carga são omitidos.

As cargas usam modo estrito no MySQL. Avisos durante INSERT interrompem e revertem a transação pendente para evitar conversões/truncamentos silenciosos; os outros objetos podem continuar conforme a opção escolhida. As linhas no contador principal só entram após COMMIT (por tabela ou por lote, conforme a configuração); o contador em andamento ainda não está confirmado.

Índices só são criados em tabelas cuja carga foi confirmada. FKs só são criadas quando ambas as tabelas foram carregadas nesta execução. Se uma tabela foi pulada ou falhou, a FK dependente fica **pulada com motivo**, para revisão manual. Não se presume que uma tabela existente contém o esquema ou os dados corretos. Isso evita alterar tabelas preservadas ou associar dados sem verificação.

Estados finais: **concluído**, **concluído com pendências** (pulos/avisos), **concluído com erros** (falhas isoladas) e **interrompido**. Sempre revise também os avisos do plano e os objetos manuais, mesmo no estado concluído. O botão de relatório exporta JSON com opções, resultados, erros, contagens, horários, avisos do plano e DDL retornado; esse relatório permanece disponível mesmo após gerar outro plano.

DDL do MySQL faz commit implícito: uma falha pode deixar tabelas vazias ou parciais. Cada carga de tabela é uma transação no modo padrão; tabelas anteriores permanecem confirmadas. Se houver perda de conexão no COMMIT, o resultado pode ser incerto: inspecione o destino antes de repetir. A aplicação nunca apaga ou trunca tabelas. Para repetir tabelas incompletas, use um banco novo ou remova manualmente apenas os artefatos da tentativa anterior após revisão. Não existe retomada automática; use o cancelamento cooperativo descrito abaixo. Não mate o processo durante uma migração.

Uso previsto: uma pessoa e uma migração por vez, na máquina local. Estado, plano, conexões e histórico ficam em memória e se perdem ao encerrar o processo. Baixe os relatórios antes de fechar. O serviço é limitado a 127.0.0.1, valida Host e usa proteção CSRF; não o exponha na rede como serviço multiusuário. Diagnósticos exibem mensagens dos drivers sem SQLAlchemy SQL/parâmetros e com ocultação de credenciais e valores de registros. Revise nomes de objetos antes de compartilhar relatórios. O cancelamento é cooperativo, entre operações de banco.

## Testes

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

No Windows, substitua `.venv/bin/python` por `.venv\Scripts\python.exe`. Os testes verificam conversões de tipos, fluxo HTTP, bloqueio CSRF, preservação de tabelas existentes, cópia em lotes e contagens usando SQLite, e compilação de DDL MySQL. Não substituem testes integrados SQL Server 2008 → MySQL 8, que exigem servidores acessíveis e credenciais.

## Diagnóstico de falhas no plano

Se a geração do plano falhar, a interface mantém um quadro com referência do erro, etapa, objeto e mensagem original do driver (quando disponível). A mesma mensagem é registrada no terminal. Copie esse diagnóstico ao relatar uma falha: ele distingue erros de catálogo, reflexão, conversão e compilação do DDL. Uma conta administradora não resolve falhas de compatibilidade ou de metadados. Credenciais conhecidas da origem são ocultadas e a exceção SQLAlchemy não é exibida com SQL e parâmetros. O diagnóstico pode conter nomes de objetos; revise antes de compartilhar.

## Checagem prévia do dicionário e adaptação do tamanho da linha

A geração do plano consulta `sys.columns`, `sys.types`, `sys.tables` e `sys.schemas` no SQL Server 2008+ para obter tipo declarado, tipo-base (inclusive aliases), comprimento máximo em bytes, precisão, escala, nulabilidade, identity, cálculo e collation. Usa parâmetros para schema/tabela. NVARCHAR/NCHAR têm comprimento em bytes dividido por dois para obter caracteres; `MAX` não é reduzido. A reflexão também fornece PKs, índices, UNIQUEs e FKs.

Quando o destino está conectado, consulta `VERSION()` e `@@innodb_page_size`. O plano usa InnoDB com **ROW_FORMAT=DYNAMIC** e **utf8mb4 (até 4 bytes/caractere)**. Na ausência dessa consulta, informa a hipótese de MySQL 8 com página de 16 KiB; gere novamente com o destino conectado para confirmar. Versões diferentes de MySQL 8/9 recebem aviso para revisão. A página de 4/8/16 KiB corresponde a orçamento de índice de 768/1536/3072 bytes; páginas maiores permanecem em 3072.

Para antecipar o **erro 1118**, estima o tamanho lógico máximo declarado por linha, com margem (orçamento de 60.000 bytes contra limite SQL de 65.535), e o registro local do InnoDB DYNAMIC. Este segundo cálculo aproxima ponteiros externos de campos variáveis grandes, metadados e campos fixos; não é uma simulação exata do InnoDB.

Se a linha exceder um dos orçamentos, tenta ampliar colunas **sem chaves/índices** de VARCHAR para TEXT/MEDIUMTEXT/LONGTEXT, e VARBINARY para BLOB/MEDIUMBLOB/LONGBLOB. Escolhe capacidade suficiente para o máximo declarado, considerando utf8mb4, e começa pelas maiores colunas. Nenhum conteúdo é cortado nem o comprimento é reduzido com base nos dados atuais. A conversão para LOB pode mudar as características de armazenamento e desempenho; revise a lista de ajustes. A DDL já é gerada com os tipos adaptados.

Colunas de PK, UNIQUE, índices e dos dois lados de FKs selecionadas são protegidas; colunas locais de FKs fora da seleção também não são transformadas. A aplicação não cria prefixos automaticamente nem remove essas regras. Se o limite continuar excedido, bloqueia a execução e indica a tabela para revisão. Não altera tabelas já existentes: o modo pular continua preservando-as integralmente.

A checagem também antecipa índices longos (1071), TEXT/BLOB indexados sem prefixo (1170), excesso de colunas no InnoDB (1017), excesso de campos em índices (16), tipos diferentes nos lados de uma FK (3780) e DECIMAL com escala superior a 30 ou precisão superior a 65. Chaves não são encurtadas e números não são arredondados automaticamente para contornar esses limites. Binários de tamanho finito são mantidos como BINARY/VARBINARY quando cabem; `MAX`/IMAGE permanecem LONGBLOB.

Na interface, abra **Checagem de compatibilidade e ajustes da DDL** e **Dicionário de dados consultado**. O ZIP do plano inclui `compatibilidade.json`; o relatório da execução também mantém essa análise. O mapeamento de colunas e o SQL exibem os tipos finais.

**Limites da análise:** não varre os registros nem garante que todo valor será aceito pelo servidor. Valores pequenos que permaneçam inline, configuração/versão do MySQL, packet size, collations, regras de UNIQUE, datas anteriores a 1000 e outras diferenças semânticas ainda podem causar falhas durante a carga. Tipos desconhecidos/espaciais e DATETIMEOFFSET continuam exigindo decisão manual. A carga estrita e o relatório por objeto permanecem ativos. Valide uma cópia de teste no MySQL real antes de usar o banco migrado.

## Ajuste manual do tipo de destino por coluna

`BigInteger`/`SmallInteger` do SQLAlchemy são reconhecidos automaticamente como BIGINT/SMALLINT. Quando outro tipo exigir mapeamento manual, a coluna continua visível na seção **Mapeamento de colunas · ajustes manuais**, junto com o tipo de origem, tipo automático disponível e motivo da pendência.

Informe o tipo no campo **Tipo manual** (há sugestões), por exemplo `BIGINT`, `BIGINT UNSIGNED`, `VARCHAR(255)`, `LONGTEXT`, `DECIMAL(18,2)`, `VARBINARY(255)`, `DATETIME(6)` ou `DATE`. Clique em **Aplicar ajustes e gerar plano novamente**. O novo plano inclui a coluna, gera nova DDL e repete todas as checagens de tamanho de linha, índices e FKs. Sem bloqueios, a migração segue pelo botão normal. Alterações não aplicadas desabilitam a execução na interface; o servidor também invalida planos anteriores ao gerar um novo.

Deixe o campo vazio para voltar ao automático, ou use **Limpar todos os ajustes** e regenere. As escolhas ficam nesta sessão da interface; recarregar a página/encerrar a aplicação não é uma configuração persistente. Os ajustes aplicados são incluídos no relatório da execução e em `compatibilidade.json` no ZIP do plano.

O servidor aceita apenas tipos de uma lista controlada, com comprimentos/precisão/escala válidos; o campo não aceita SQL, cláusulas DEFAULT, COLLATE ou comandos arbitrários. Tipos manuais não são sobrescritos pelo ajuste automático de tamanho de linha. Uma escolha incompatível continua bloqueando o plano até ser corrigida, inclusive TEXT em PK/índice sem prefixo ou números diferentes nos lados de uma FK.

O ajuste define **o tipo da coluna na DDL**, não uma transformação dos valores. Reduzir tamanhos, precisão/escala, trocar texto por número ou escolher DATETIME para DATETIMEOFFSET pode exigir preparação adicional dos dados e decisão sobre fuso horário. A carga estrita continua rejeitando valores incompatíveis. A origem e as tabelas existentes no destino não são alteradas por essa edição.

## Desempenho, acompanhamento e controle de grandes cargas

A cópia usa INSERT em lote (executemany), reutiliza o comando de inserção e calcula as colunas UUID uma vez por tabela. O cursor PyMySQL acumula avisos inclusive entre sublotes internos: SHOW WARNINGS só é consultado se houver aviso (ou se o driver não disponibilizar a contagem), evitando uma ida ao servidor por lote sem avisos. A carga estrita permanece habilitada. Índices secundários e FKs continuam depois da carga.

Em **Desempenho e validação de tabelas grandes**, configure:

- **Máximo de linhas por lote**: até 50.000, sugestão inicial 5.000 na interface. O primeiro lote amostra uma linha; os seguintes se adaptam ao tamanho médio observado. Durante a execução, ajuste o máximo no monitor, sem reiniciar a cópia. A alteração vale para as próximas leituras; o lote já em memória pode usar a configuração anterior.
- **Orçamento aproximado em MiB**: de 1 a 64, padrão 8. Lotes são divididos pelo volume aproximado e limitados também a uma fração de max_allowed_packet. Não é limite rígido de memória ou pacote: uma linha individual não pode ser dividida, a estimativa muda com a distribuição dos dados e o driver tem buffers próprios. Um LOB individual maior que o limite do servidor exige ajuste de max_allowed_packet ou tratamento específico.
- **Uma transação por tabela** (padrão): falha/cancelamento reverte a carga pendente inteira da tabela atual. Tabelas anteriores permanecem. Transações grandes podem consumir log, manter bloqueios e demorar no COMMIT.
- **COMMIT por lote** (opcional): reduz o tamanho das transações e confirma progresso durante a carga; pode melhorar certos cenários, mas adicionar COMMITs também pode reduzir velocidade. Falha/cancelamento preserva os lotes já confirmados e deixa a tabela parcial, sem índices/FKs posteriores. Não há retomada automática nem nova inserção em tabelas existentes. Inspecione o relatório antes de repetir.
- **COUNT(*) no destino**: habilitado por padrão, compara a contagem final às linhas enviadas. Pode exigir uma varredura longa em tabelas grandes. Desmarcar evita essa consulta, mas limita a validação à execução dos INSERTs e COMMITs; o relatório identifica explicitamente `commits_only`, sem afirmar contagem independente.

O monitor informa linhas enviadas, confirmadas e pendentes por tabela, lotes, tamanho do último lote, linhas/segundo e tempos acumulados de SELECT/leitura, preparação, INSERT/avisos, COMMIT e validação. A velocidade considera o tempo ativo observado e não é garantia do desempenho futuro. O total e o ETA usam `sys.partitions` (metadados SQL Server, sem COUNT na origem), são aproximados e podem mudar com escritas concorrentes; mantenha a origem sem escritas. Se o catálogo não fornecer a estimativa, a carga continua sem percentual/ETA. Esses indicadores cobrem a carga da tabela, não o tempo futuro de criação de índices/FKs.

A atividade em andamento e seu tempo aparecem mesmo enquanto o banco não responde. Isso permite distinguir leitura lenta, INSERT, COMMIT, COUNT e DDL. Um tempo longo não prova travamento. O monitor evita reenviar dicionários de dados e DDLs antigos a cada atualização; resultados por objeto chegam de forma incremental. O relatório JSON continua completo. Recarregar a página recupera o monitor de uma execução ativa no mesmo processo, inclusive pausada.

**Pausar**, **Continuar** e **Cancelar** funcionam em pontos de controle entre operações/lotes. Não interrompem um SELECT, INSERT, COMMIT, COUNT ou DDL já em andamento. O cancelamento acorda uma execução pausada e reverte apenas a transação ainda pendente; COMMITs anteriores e DDLs permanecem. Em perda de conexão durante COMMIT, a confirmação pode ser incerta: confira o destino. Pausar mantém conexões e pode manter transações/bloqueios; evite pausas prolongadas e monitore o banco. Enquanto pausada/cancelando, a aplicação bloqueia alterações do plano/conexões e novas migrações.

O timeout de operações é configurável nas conexões, de 10 a 3.600 segundos (padrão 300). Ele se aplica às operações do driver, não é um prazo máximo para toda a migração nem uma garantia de duração do cancelamento. A aplicação usa apenas um fluxo de cópia por vez; não desabilita UNIQUE, integridade ou durabilidade para aumentar velocidade. Gargalos de disco, CPU, rede, bloqueios e configuração dos bancos ainda exigem avaliação no ambiente real. Use as métricas para comparar configurações com uma amostra representativa, mantendo o mesmo nível de validação.

## Destinos MySQL, PostgreSQL e Oracle

O formulário **Banco de destino** seleciona o perfil, a porta sugerida, o campo banco/service name, o schema e as sugestões de tipos manuais. Mudar o destino invalida o plano da interface e limpa seus ajustes de tipos: conecte o novo destino e gere novamente. O servidor também recusa um plano criado para outro dialeto. O estado da origem continua SQL Server, inclusive versão 2008/2008 R2.

Todos os destinos mantêm: inventário e dicionário parametrizado do SQL Server; plano revisável; ajustes manuais por coluna com validação e sem aceitar SQL arbitrário; criação e confirmação por objeto no catálogo; opção de pular tabelas/views existentes; falhas isoladas conforme configuração; carga adaptativa; orçamento aproximado de memória; transação por tabela ou por lote; COUNT opcional; pausa, cancelamento e ajuste de lote; relatório completo e monitor incremental. Objetos existentes não são reutilizados nem modificados; pular não completa uma tabela parcial. FKs que dependam de tabelas puladas/falhas continuam sinalizadas para revisão.

Instale as novas dependências antes de iniciar:

```bash
python -m pip install -r requirements.txt
```

No ambiente virtual Windows, use `.venv\Scripts\python.exe -m pip install -r requirements.txt`; no Linux/macOS, `.venv/bin/python -m pip install -r requirements.txt`. PostgreSQL usa psycopg 3 com pacote binário. Oracle usa python-oracledb em modo **thin** e não exige Instant Client; a conexão deste modo requer Oracle 12.1+ e autenticação compatível. Oracle 11g, SID em lugar de service name, modo thick, SYSDBA, external authentication e wallets/MTLS que exijam configuração específica não são configurados pela interface desta versão.

### PostgreSQL

- Informe host, porta (sugestão 5432), banco, usuário, senha e schema já existente (padrão `public`). Use um schema dedicado e conceda USAGE e CREATE ao usuário; ele deve poder inserir, contar e alterar os objetos que criar. Não são criados bancos, usuários ou schemas automaticamente.
- TLS marcado usa `sslmode=verify-full`; configure uma CA confiável no campo opcional de caminho local, ou pelo mecanismo padrão do libpq. Com TLS desmarcado, usa `prefer`, que pode permitir fallback sem criptografia. O timeout configura `statement_timeout` por operação no servidor.
- O perfil consulta versão, block_size e encoding; UTF8 é recomendado. Nomes gerados respeitam 63 bytes com hash e os objetos são qualificados pelo schema. Colunas acima de 63 bytes e nomes de colunas de sistema são bloqueados, sem renomeação silenciosa.
- Texto grande vira TEXT, binário vira BYTEA, GUID vira UUID, BIT vira BOOLEAN, DECIMAL usa NUMERIC (incluindo escala 38 quando válida), DATETIMEOFFSET vira TIMESTAMP WITH TIME ZONE. Strings de hora/data com fuso precisam conter offset explícito; não é assumido o fuso da máquina.
- TOAST é nativo para valores variáveis: não se aplica a regra MySQL de 65.535 bytes e não é aplicado ROW_FORMAT. A checagem estima limite local de tupla, quantidade de colunas, chaves e compatibilidade de FKs. Índices btree sobre TEXT/BYTEA podem falhar conforme o tamanho real dos dados; o plano sinaliza essa limitação.
- PostgreSQL rejeita caractere NUL em texto; a carga interrompe a tabela, sem retirar caracteres silenciosamente. BYTEA preserva bytes NUL. Collation e comparação não são consideradas equivalentes às da origem.

### Oracle

- Informe host, porta (sugestão 1521), **service_name** registrado no listener (não SID), usuário e senha. O schema padrão é o usuário conectado; um owner explícito é opcional e precisa existir e estar autorizado para as operações. Recomenda-se um usuário próprio para migração com CREATE SESSION, CREATE TABLE, quota de tablespace e permissões necessárias para seus objetos/relacionamentos. Não use SYS/SYSTEM como conta da aplicação.
- TLS marcado usa TCPS com verificação do nome do servidor; configure um certificado confiável e a porta correta no servidor (TCPS pode usar 2484). A interface não configura wallets ou certificados de cliente para MTLS. Desmarcado usa TCP. Timeout define call_timeout do driver por round trip.
- O perfil consulta os charsets do banco/nacional e tenta consultar db_block_size. Se a segunda consulta não estiver autorizada, usa um orçamento conservador de chave baseado em bloco de 8 KiB e avisa, sem exigir novos privilégios. As estimativas não certificam todos os limites físicos do Oracle.
- Nomes de tabelas/índices/constraints são gerados em minúsculas lógicas (não exigem aspas para nomes simples) com hash para respeitar 30 bytes, por compatibilidade conservadora. Colunas mantêm os nomes originais quando cabem; acima de 30 bytes ou `ROWID` maiúsculo ficam bloqueadas. Não é assumido suporte a identificadores estendidos ou MAX_STRING_SIZE=EXTENDED.
- Inteiros/decimais viram NUMBER, BIT vira NUMBER(1,0), Unicode/textos curtos viram NVARCHAR2 e longos/MAX viram NCLOB. Esse uso do charset nacional evita depender do charset principal para preservar Unicode. Binários pequenos viram RAW; longos viram BLOB; GUID vira NVARCHAR2(36). VARCHAR2/NVARCHAR2 manuais têm limite conservador de 1.000 caracteres; RAW, 2.000 bytes.
- DATETIME/DATETIME2 tornam-se TIMESTAMP com precisão 6. DATETIMEOFFSET usa TIMESTAMP WITH TIME ZONE com bind explícito DB_TYPE_TIMESTAMP_TZ; offset ausente é rejeitado. TIME vira INTERVAL DAY(1) TO SECOND(6), com zero dias: é uma representação de duração/hora e exige revisar regras e consultas que esperem o tipo TIME.
- **Oracle não distingue texto vazio de NULL.** Por padrão o perfil interrompe a carga ao encontrar strings ou binários vazios. É possível autorizar a conversão de textos vazios para NULL em colunas textuais que aceitem NULL, conforme a opção descrita abaixo. Binários continuam exigindo tratamento específico. LOBs vazios também exigem tratamento específico, não presumido pelo carregador.
- LOBs não entram em índices/FKs convencionais. UNIQUEs de índices simples da origem são emitidos como constraints UNIQUE, para que possam ser referenciados por FKs no Oracle. Isso pode antecipar sua validação para a fase de carga. ON UPDATE com ação de propagação não é silenciosamente descartado: o plano bloqueia e informa a incompatibilidade.

### Confirmação, relatórios e transações nos novos destinos

Após DDL, todos os perfis confirmam COMMIT e verificam o objeto no catálogo. MySQL mantém SHOW CREATE TABLE e avisos de SHOW WARNINGS. PostgreSQL/Oracle mostram os metadados retornados pelo Inspector — colunas/tipos, nulabilidade, PK, UNIQUEs, índices e FKs — sem requerer DBMS_METADATA ou apresentar DDL reconstruída como se fosse o retorno literal do servidor. O ZIP tem `mysql-plan.sql`, `postgresql-plan.sql` ou `oracle-plan.sql`, conforme o destino. A análise de compatibilidade e o relatório registram o tipo de banco e o schema.

MySQL e Oracle têm COMMIT implícito de DDL. PostgreSQL suporta DDL transacional, mas esta aplicação confirma **cada objeto separadamente** para manter o relatório e isolar falhas; o fluxo completo não é uma única transação. Cancelar não desfaz DDLs ou lotes anteriores. O modo por tabela continua revertendo a carga pendente da tabela; o modo por lote pode deixar cargas parciais em todos os destinos.

Erros Oracle incluem código ORA quando fornecido; PostgreSQL inclui SQLSTATE. Há orientações para duplicidade, nulabilidade, encoding, precisão/tamanho, integridade referencial, privilégios, quota e timeout. Valores dos registros não são expostos nos diagnósticos da carga. Informações de credenciais permanecem na memória do processo.

### Escopo e validação

A conversão de views, procedures, funções, triggers, sequences/synonyms, defaults, colunas calculadas, geradores IDENTITY/SEQUENCE e CHECKs de negócio continua manual, em todos os destinos. FKs com SET DEFAULT ficam bloqueadas também no PostgreSQL, pois os defaults não são recriados por este fluxo. SET DEFAULT em FKs fica bloqueado em todos os destinos, pois os defaults da origem não são recriados automaticamente. O sistema copia valores e não promete equivalência automática de T-SQL, collation, regras de NULL, fusos ou precisão de sete casas temporais. Os perfis não alteram parâmetros de servidor para contornar limites.

A suíte cobre compilação independente dos três dialetos, tipos, metadados de destino simulados, API, parser manual, compatibilidade, preservação de objetos, controle/transações e cópia em lotes com SQLite. Os drivers são instalados e as factories de conexão são verificadas sem conectar. **Não houve acesso a servidores PostgreSQL/Oracle reais neste ambiente**: valide conexão, permissões, catálogo, LOBs, Unicode, datas/fusos, grandes cargas e regras de negócio com uma cópia de teste antes de usar o destino.

## SQL Server: erro 102 / sintaxe próxima de CAST na reflexão

O leitor padrão de metadados do SQLAlchemy pode gerar consultas de comentários com `fn_listextendedproperty` e argumentos convertidos por CAST. Dependendo do SQL Server, nível de compatibilidade e driver, esse caminho pode falhar mesmo com um usuário administrador. O erro de sintaxe, por si só, não indica falta de permissão.

As conexões de origem desabilitam a reflexão de comentários estendidos (`supports_comments=False`), que não é necessária para migrar tabelas e valores. As definições de objetos continuam sendo exportadas do catálogo.

No modo **Automática com fallback de compatibilidade**, uma falha de sintaxe 102/156 ao refletir metadados aciona o leitor direto de `sys.columns`, `sys.types`, `sys.key_constraints`, `sys.indexes`, `sys.foreign_keys` e catálogos relacionados, com filtros parametrizados e sem essa chamada de comentários. A tabela parcialmente refletida é descartada antes da nova tentativa. O plano mostra o modo usado e o código do fallback recuperado; o relatório de compatibilidade também o registra.

Para forçar essa leitura, selecione **Catálogo compatível (SQL Server 2008+)** na conexão de origem, teste/conecte novamente, leia o catálogo e gere um novo plano. Essa opção também se aplica ao inventário e à leitura de índices/constraints. Mantém tipos-base e aliases, tamanhos, nulabilidade, ordem de PK composta, índices simples/UNIQUE, FKs e as informações de defaults, identity e colunas calculadas usadas nos avisos do plano. Comentários estendidos não são necessários nem migrados por esse caminho. Cálculos/defaults/identity/CHECKs continuam com as mesmas limitações de conversão já documentadas.

A aplicação não modifica compatibilidade do banco, permissões, objetos ou dados da origem. Erros de autenticação, permissões (por exemplo 229), rede e tipos não são tratados como erros de sintaxe e continuam sendo reportados. O catálogo exige visibilidade de metadados; se uma definição calculada necessária estiver invisível, o plano informa a ausência. Se o próprio leitor de catálogo falhar, a causa é exibida: o fallback não garante corrigir todo erro 102 em qualquer contexto, especialmente fora da reflexão.

## Oracle: opção para converter textos vazios em NULL

A mensagem `valor vazio não pode ser preservado como distinto de NULL` sinaliza uma diferença entre os bancos: SQL Server distingue `''` de NULL; Oracle não preserva essa distinção em seus tipos de texto usuais. A aplicação mantém a política conservadora por padrão.

Após gerar o plano com Oracle como destino, escolha em **Oracle · tratamento de textos vazios**:

- **Interromper ao encontrar texto vazio**: padrão; não transforma os valores.
- **Converter textos vazios para NULL em colunas que aceitem NULL**: autorização explícita para substituir somente a string exatamente vazia por NULL no payload enviado ao Oracle. A origem permanece intacta. A opção é registrada na execução, e a conclusão com conversões recebe o estado de pendências/avisos para revisão.

A conversão exige tipo textual no destino (VARCHAR2/NVARCHAR2/CHAR/NCHAR/CLOB/NCLOB etc.) e nulabilidade confirmada. **PKs e colunas NOT NULL não são relaxadas automaticamente**: o erro identifica a coluna e pede um valor de negócio definido na preparação dos dados. Escolher NULL não resolve um campo obrigatório. Texto vazio destinado a NUMBER/data ou outro tipo não textual também exige transformação específica.

Não há trim, substituição por espaço, zero, data padrão ou palavra `NULL`. Strings com espaços, textos não vazios e NULLs já existentes permanecem iguais. Binários vazios (RAW/BLOB, inclusive bytes vazios) não são convertidos por essa opção. MySQL e PostgreSQL continuam preservando texto vazio como antes.

O monitor e o JSON da execução mostram **empty_to_null_prepared** e **empty_to_null_confirmed**, com contagem de valores por tabela/coluna, sem expor os registros. Preparados são tentativas de transformação em memória, inclusive lotes que podem não chegar a ser gravados; confirmados só aumentam depois de COMMIT bem-sucedido. Em reversão por tabela, confirmados ficam zerados; no modo por lote, conversões de lotes anteriores confirmados permanecem contabilizadas mesmo se a tabela falhar depois. Uma perda de conexão durante COMMIT continua exigindo inspeção do destino.

Para tentar novamente após uma falha, revise primeiro as tabelas criadas e os COMMITs existentes. O modo pular preserva tabelas existentes e não completa cargas parciais; use um banco/schema de teste novo ou remova manualmente somente os artefatos da tentativa anterior após avaliação. Não há retomada automática de tabela parcial.
