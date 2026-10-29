# SQLBridge — SQL Server → MySQL

Aplicação web local em Python 3.11+ com Flask, SQLAlchemy, pymssql (FreeTDS) e PyMySQL. Preparada para catálogo do **SQL Server 2008 (10.0) / 2008 R2 (10.50)** e destino **MySQL 8**. Não usa ODBC nem precisa publicar um site. A compatibilidade com seu servidor deve ser confirmada com uma migração de teste: não foi possível acessar bancos reais neste ambiente.

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

As cargas usam modo estrito no MySQL. Avisos durante INSERT interrompem e revertem a transação da tabela para evitar conversões/truncamentos silenciosos; os outros objetos podem continuar conforme a opção escolhida. As linhas no contador principal só entram após COMMIT da tabela; o contador em andamento ainda não está confirmado.

Índices só são criados em tabelas cuja carga foi confirmada. FKs só são criadas quando ambas as tabelas foram carregadas nesta execução. Se uma tabela foi pulada ou falhou, a FK dependente fica **pulada com motivo**, para revisão manual. Não se presume que uma tabela existente contém o esquema ou os dados corretos. Isso evita alterar tabelas preservadas ou associar dados sem verificação.

Estados finais: **concluído**, **concluído com pendências** (pulos/avisos), **concluído com erros** (falhas isoladas) e **interrompido**. Sempre revise também os avisos do plano e os objetos manuais, mesmo no estado concluído. O botão de relatório exporta JSON com opções, resultados, erros, contagens, horários, avisos do plano e DDL retornado; esse relatório permanece disponível mesmo após gerar outro plano.

DDL do MySQL faz commit implícito: uma falha pode deixar tabelas vazias ou parciais. Cada carga de tabela é uma transação; tabelas anteriores permanecem confirmadas. Se houver perda de conexão no COMMIT, o resultado pode ser incerto: inspecione o destino antes de repetir. A aplicação nunca apaga ou trunca tabelas. Para repetir tabelas incompletas, use um banco novo ou remova manualmente apenas os artefatos da tentativa anterior após revisão. Não existe retomada automática nem cancelamento. Não mate o processo durante uma migração.

Uso previsto: uma pessoa e uma migração por vez, na máquina local. Estado, plano, conexões e histórico ficam em memória e se perdem ao encerrar o processo. Baixe os relatórios antes de fechar. O serviço é limitado a 127.0.0.1, valida Host e usa proteção CSRF; não o exponha na rede como serviço multiusuário. Diagnósticos exibem mensagens dos drivers sem SQLAlchemy SQL/parâmetros e com ocultação de credenciais e valores de registros. Revise nomes de objetos antes de compartilhar relatórios. Não há cancelamento nesta versão.

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
