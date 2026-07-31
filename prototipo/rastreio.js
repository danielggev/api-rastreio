/**
 * Consulta de rastreio -- logica da interface.
 *
 * Sem framework e sem dependencia: este arquivo pode ser colado tal como esta
 * dentro de um `<script>` no tema da Shopify depois.
 *
 * SCRIPT CLASSICO, de proposito -- nao e modulo ES. Navegadores BLOQUEIAM
 * `import` quando a pagina e aberta via `file://`, e o sintoma e traicoeiro: o
 * arquivo simplesmente nao carrega, o formulario fica sem listener e o submit
 * recarrega a pagina sem erro visivel. Como este prototipo precisa abrir com um
 * duplo clique, expomos a funcao no `window` em vez de exportar.
 *
 * REGRA DE SEGURANCA: todo dado vindo da API e inserido com `textContent`,
 * NUNCA com `innerHTML`. Os campos `rotulo` e `descricao` vem da Frete Rapido e
 * das transportadoras -- se um dia qualquer um deles trouxer markup, `innerHTML`
 * o executaria no navegador do cliente.
 */

(function () {
"use strict";

function urlApi() {
  return window.RASTREIO_API || "http://localhost:8000/api/v1/rastreio";
}

/** Grupos que exigem ACAO do cliente -- e onde a pagina evita um contato no suporte. */
const EXIGEM_ACAO = new Set(["aguardando_retirada", "tentativa_falha"]);

/** Texto de apoio por grupo. O rotulo vem da API; isto explica o que fazer. */
const ORIENTACAO = {
  aguardando_retirada:
    "Sua encomenda esta disponivel para retirada. Se ninguem buscar dentro do " +
    "prazo, ela volta para a loja.",
  tentativa_falha:
    "A entrega foi tentada e nao deu certo. Normalmente a transportadora tenta " +
    "de novo nos proximos dias uteis.",
  devolucao: "Sua encomenda esta voltando para a loja.",
  sinistro: "Houve um problema com sua encomenda durante o transporte.",
  cancelado: "Este envio foi cancelado.",
  saiu_para_entrega: "Sua encomenda saiu para entrega e deve chegar hoje.",
  entregue: "Sua encomenda foi entregue.",
};

// ---------------------------------------------------------------------------
// Formatacao
// ---------------------------------------------------------------------------

/** "2026-07-28T17:56:48-03:00" -> "28/07/2026 as 17:56" */
function formatarDataHora(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const data = d.toLocaleDateString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  const hora = d.toLocaleTimeString("pt-BR", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${data} as ${hora}`;
}

/** "2026-07-29" -> "29/07/2026". Data pura: sem fuso, para nao deslocar o dia. */
function formatarData(iso) {
  if (!iso) return null;
  const [ano, mes, dia] = iso.split("-");
  if (!ano || !mes || !dia) return null;
  return `${dia}/${mes}/${ano}`;
}

// ---------------------------------------------------------------------------
// Construcao do DOM (sempre com textContent)
// ---------------------------------------------------------------------------

function elemento(tag, classe, texto) {
  const el = document.createElement(tag);
  if (classe) el.className = classe;
  if (texto !== undefined && texto !== null) el.textContent = texto;
  return el;
}

function bloco(titulo, valor) {
  if (!valor) return null; // campos opcionais podem vir nulos mesmo em sucesso
  const div = elemento("div", "rastreio-info");
  div.append(elemento("span", "rastreio-info__rotulo", titulo));
  div.append(elemento("span", "rastreio-info__valor", valor));
  return div;
}

function montarSucesso(dados) {
  const raiz = elemento("div", "rastreio-resultado");
  const atual = dados.status_atual;

  raiz.append(elemento("p", "rastreio-pedido", `Pedido ${dados.pedido}`));

  // Status atual em destaque, com a cor vindo do grupo.
  const destaque = elemento("div", `rastreio-status rastreio-status--${atual.grupo}`);
  destaque.append(elemento("strong", "rastreio-status__rotulo", atual.rotulo));
  const quando = formatarDataHora(atual.data);
  if (quando) destaque.append(elemento("span", "rastreio-status__data", quando));
  raiz.append(destaque);

  // Orientacao: e o que transforma "Disponivel para retirada" em algo acionavel.
  const orientacao = ORIENTACAO[atual.grupo];
  if (orientacao) {
    const aviso = elemento(
      "p",
      EXIGEM_ACAO.has(atual.grupo) ? "rastreio-aviso rastreio-aviso--acao" : "rastreio-aviso",
      orientacao
    );
    raiz.append(aviso);
  }

  if (dados.entrega_atrasada) {
    raiz.append(
      elemento(
        "p",
        "rastreio-aviso rastreio-aviso--atraso",
        `A previsao era ${formatarData(dados.previsao_entrega)} e a entrega esta atrasada.`
      )
    );
  }

  const infos = elemento("div", "rastreio-infos");
  const transportadora = bloco("Transportadora", dados.transportadora);
  if (transportadora) infos.append(transportadora);

  // A previsao so interessa a quem AINDA espera. Omitimos em dois casos:
  // - entregue: o pedido chegou, e a previsao vencida so apontaria um atraso
  //   para quem ja recebeu;
  // - atrasado: o aviso acima ja menciona a data, repetir seria redundante.
  const entregue = atual.grupo === "entregue";
  if (!dados.entrega_atrasada && !entregue) {
    const previsao = bloco("Previsao de entrega", formatarData(dados.previsao_entrega));
    if (previsao) infos.append(previsao);
  }
  if (infos.childElementCount > 0) raiz.append(infos);

  // Linha do tempo, mais recente primeiro (a API ja entrega ordenada).
  if (dados.historico && dados.historico.length > 0) {
    raiz.append(elemento("h3", "rastreio-historico__titulo", "Historico"));
    const lista = elemento("ol", "rastreio-historico");
    for (const evento of dados.historico) {
      const item = elemento("li", `rastreio-evento rastreio-evento--${evento.grupo}`);
      item.append(elemento("span", "rastreio-evento__rotulo", evento.rotulo));
      const data = formatarDataHora(evento.data);
      if (data) item.append(elemento("span", "rastreio-evento__data", data));
      if (evento.descricao) {
        item.append(elemento("span", "rastreio-evento__descricao", evento.descricao));
      }
      lista.append(item);
    }
    raiz.append(lista);
  }

  return raiz;
}

function montarMensagem(dados, modificador) {
  const raiz = elemento("div", `rastreio-resultado rastreio-resultado--${modificador}`);
  if (dados.pedido) {
    raiz.append(elemento("p", "rastreio-pedido", `Pedido ${dados.pedido}`));
  }
  raiz.append(elemento("p", "rastreio-mensagem", dados.mensagem));
  return raiz;
}

// ---------------------------------------------------------------------------
// Fluxo
// ---------------------------------------------------------------------------

function renderizar(alvo, resposta, dados) {
  alvo.replaceChildren();

  // 422: erro de validacao do proprio formulario (email malformado).
  if (resposta.status === 422) {
    alvo.append(
      montarMensagem(
        { mensagem: "Confira o email digitado: o formato parece invalido." },
        "erro"
      )
    );
    return;
  }

  switch (dados.resultado) {
    case "sucesso":
      alvo.append(montarSucesso(dados));
      break;
    case "sem_rastreio":
      alvo.append(montarMensagem(dados, "aguardando"));
      break;
    case "vazio_fr":
      alvo.append(montarMensagem(dados, "indisponivel"));
      break;
    case "nao_encontrado":
      alvo.append(montarMensagem(dados, "nao-encontrado"));
      break;
    case "limite_excedido":
      alvo.append(montarMensagem(dados, "limite"));
      break;
    case "erro_externo":
    default:
      alvo.append(
        montarMensagem(
          { mensagem: dados.mensagem || "Nao foi possivel consultar agora." },
          "erro"
        )
      );
  }
}

function ligarFormulario({ formulario, campoEmail, campoPedido, resultado, botao }) {
  if (!formulario || !campoEmail || !campoPedido || !resultado) {
    // Falha de integracao com o HTML: melhor gritar no console do que deixar o
    // formulario silenciosamente inerte.
    console.error("[rastreio] elementos do formulario nao encontrados", {
      formulario, campoEmail, campoPedido, resultado,
    });
    return;
  }

  let emAndamento = false;

  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    if (emAndamento) return; // evita duplo envio consumindo o rate limit
    emAndamento = true;

    if (botao) botao.disabled = true;
    resultado.replaceChildren(
      elemento("p", "rastreio-carregando", "Consultando seu pedido...")
    );

    try {
      const resposta = await fetch(urlApi(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: campoEmail.value.trim(),
          // O "#" e tolerado pela API, mas limpar aqui evita ida e volta a toa.
          numero_pedido: campoPedido.value.trim().replace(/^#/, ""),
        }),
      });

      let dados = {};
      try {
        dados = await resposta.json();
      } catch {
        dados = {};
      }

      renderizar(resultado, resposta, dados);
    } catch {
      // Falha de rede: o fetch nem chegou ao servidor.
      resultado.replaceChildren(
        montarMensagem(
          { mensagem: "Nao conseguimos conectar. Verifique sua internet e tente de novo." },
          "erro"
        )
      );
    } finally {
      emAndamento = false;
      if (botao) botao.disabled = false;
    }
  });
}

// Exposto no window porque este e um script classico (ver nota no topo).
window.Rastreio = { ligarFormulario: ligarFormulario };

})();
